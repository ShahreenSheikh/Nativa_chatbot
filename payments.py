"""
Manual payment handling for NativaCare — bank transfer + screenshot proof
+ staff approval, instead of a real payment gateway.

Flow:
  1. Booking reaches the payment step -> create_invoice() persists an
     Invoice row (with the full booking "lead" attached as JSON) and
     returns bank details + a reference code. This happens immediately,
     independent of the chat session, so it survives the session expiring,
     the tab closing, or the server restarting.
  2. Patient sends a screenshot — via the website upload endpoint or a
     WhatsApp image — which is stored and linked to the invoice
     (add_payment_proof, called from main.py).
  3. Staff reviews it on the dashboard's Payments page and calls
     approve_invoice() or reject_invoice().
  4. approve_invoice() is what actually creates the appointment, writes
     the calendar event, and sends confirmation emails — it does NOT
     depend on the original chat session still existing.

Bank account configuration (.env):
    BANK_ACCOUNTS_JSON — a JSON array of accounts, e.g.:
      [{"label": "AED - Mashreq", "bank_name": "Mashreq Bank",
        "account_name": "NativaCare Midwifery LLC",
        "account_number": "0123456789", "iban": "AE07...",
        "currency": "AED"}]

    If BANK_ACCOUNTS_JSON isn't set, falls back to individual
    BANK_NAME / BANK_ACCOUNT_NAME / BANK_ACCOUNT_NUMBER / BANK_IBAN vars
    as a single account — simplest setup for a clinic with just one
    account. Either way, get_bank_accounts() always returns a list, so
    supporting a second account later is just adding a row to the JSON,
    no code change.

Public API:
    get_bank_accounts() -> list[dict]
    create_invoice_for_lead(session_id, channel, contact, lead) -> dict
    approve_invoice(invoice_id, approved_by="") -> dict
    reject_invoice(invoice_id, reason="", rejected_by="") -> dict
    format_invoice_message(invoice) -> str
"""

import os
import json
from dotenv import load_dotenv

import db
from models import AppointmentLead

load_dotenv()

CLINIC_NAME = os.getenv("CLINIC_NAME", "NativaCare")
FRONTEND_BASE_URL = os.getenv("FRONTEND_BASE_URL", "").rstrip("/")
if FRONTEND_BASE_URL:
    print(f"[Payments startup] FRONTEND_BASE_URL is set — invoices will include "
          f"a clickable upload link: {FRONTEND_BASE_URL}/pay/{{reference}}")
else:
    print("[Payments startup] WARNING: FRONTEND_BASE_URL is NOT set — invoices "
          "will say 'send it in this chat' instead of giving a clickable "
          "upload link. Set this in .env to the backend's own public URL.")

# Clinic owner's own WhatsApp number (personal, not the bot's number) —
# a message goes here whenever a patient uploads a payment screenshot, so
# staff don't have to remember to check the dashboard. Format: digits only,
# no leading + or spaces (e.g. "971501234567"), same format WhatsApp's
# Cloud API expects for the "to" field. Leave blank to skip this.
OWNER_WHATSAPP_NUMBER = os.getenv("OWNER_WHATSAPP_NUMBER", "").strip()


async def notify_owner_of_payment_proof(invoice: dict):
    """Fire-and-forget notification (email + WhatsApp) to clinic staff the
    moment a payment screenshot comes in — via either channel (website
    upload or WhatsApp image). Best-effort: failures here are logged, not
    raised, since a notification failing should never block the upload
    itself from succeeding for the patient."""
    from email_service import send_payment_proof_notification
    # Fetch the actual screenshot so staff can see it right in the email
    # instead of needing to open the dashboard first — that's the whole
    # point of being able to approve/reject straight from the email.
    proofs = db.list_proofs_for_invoice(invoice.get("id"))
    latest_proof = proofs[0] if proofs else None
    try:
        await send_payment_proof_notification(invoice, proof=latest_proof)
    except Exception as e:
        print(f"[Payments] Owner email notification failed: {e}")

    if OWNER_WHATSAPP_NUMBER:
        try:
            from whatsapp_service import send_whatsapp_message
            lead = invoice.get("lead", {}) or {}
            reference = invoice.get("reference", "")
            msg = (
                f"💰 Payment screenshot received\n"
                f"Patient: {lead.get('patient_name', '(unknown)')}\n"
                f"Service: {lead.get('service_name', '')}\n"
                f"Amount: AED {invoice.get('amount_aed', '')}\n"
                f"Reference: {reference}\n\n"
                f"Reply \"approve {reference}\" or \"reject {reference}\" "
                f"right here to decide, or review it on the dashboard first."
            )
            await send_whatsapp_message(OWNER_WHATSAPP_NUMBER, msg)
        except Exception as e:
            print(f"[Payments] Owner WhatsApp notification failed: {e}")


def get_bank_accounts() -> list:
    raw = os.getenv("BANK_ACCOUNTS_JSON", "").strip()
    if raw:
        try:
            accounts = json.loads(raw)
            if isinstance(accounts, list) and accounts:
                return accounts
        except (TypeError, ValueError) as e:
            print(f"[Payments] BANK_ACCOUNTS_JSON is set but invalid JSON: {e}. "
                  f"Falling back to single-account env vars.")

    single = {
        "label": os.getenv("BANK_LABEL", "Bank Transfer"),
        "bank_name": os.getenv("BANK_NAME", ""),
        "account_name": os.getenv("BANK_ACCOUNT_NAME", CLINIC_NAME),
        "account_number": os.getenv("BANK_ACCOUNT_NUMBER", ""),
        "iban": os.getenv("BANK_IBAN", ""),
        "swift_code": os.getenv("BANK_SWIFT_CODE", ""),
        # Needed for anyone wiring money from OUTSIDE the UAE — a local
        # AED transfer within the UAE doesn't need it, but an
        # international/SWIFT transfer does. Optional: leave blank if
        # you only expect local transfers.
        "currency": os.getenv("BANK_CURRENCY", "AED"),
    }
    if not single["bank_name"] and not single["account_number"] and not single["iban"]:
        print("[Payments] WARNING: no bank account configured. Set "
              "BANK_ACCOUNTS_JSON or BANK_NAME/BANK_ACCOUNT_NUMBER/BANK_IBAN "
              "in .env, or invoices will show blank payment details.")
    return [single]


def _default_account() -> dict:
    accounts = get_bank_accounts()
    return accounts[0] if accounts else {}


def _account_by_label(label: str) -> dict:
    """Look up a specific configured bank account by its label — used
    when reconstructing the account details for an invoice that's
    already been created, since `_account` on the dict returned by
    create_invoice_for_lead() is a transient, display-only field (never
    persisted to the database), so a fresh fetch via db.get_invoice()
    doesn't have it. `bank_account_label` IS persisted (see the Invoice
    model), so this recovers the exact same account that was originally
    shown to the patient — important once BANK_ACCOUNTS_JSON supports
    multiple accounts, so an approved invoice's PDF doesn't silently
    show account #1 when the patient actually paid into account #2."""
    if not label:
        return _default_account()
    for account in get_bank_accounts():
        if account.get("label") == label:
            return account
    return _default_account()  # label no longer matches any configured account


def create_invoice_for_lead(session_id: str, channel: str, contact: str,
                             lead: dict) -> dict:
    """Persist an invoice for a booking lead and return it (with bank
    details already resolved for display)."""
    amount = str(lead.get("price_aed") or "")
    account = _default_account()
    inv = db.create_invoice(
        session_id=session_id,
        channel=channel,
        contact=contact,
        lead=lead,
        amount_aed=amount,
        bank_account_label=account.get("label", ""),
    )
    inv["_account"] = account
    return inv


def _proof_upload_url(invoice: dict) -> str:
    ref = invoice.get("reference", "")
    if FRONTEND_BASE_URL:
        return f"{FRONTEND_BASE_URL}/pay/{ref}"
    return ""


def format_invoice_message(invoice: dict) -> str:
    """The message shown in-chat. Bank details are in the text itself —
    never only in a PDF/download — so they survive even if the chat
    resets before the patient finishes reading it."""
    account = invoice.get("_account") or _default_account()
    lead = invoice.get("lead", {})
    service_name = lead.get("service_name") or "your appointment"
    amount = invoice.get("amount_aed") or "—"
    ref = invoice.get("reference", "")

    lines = [
        f"To confirm your {service_name} booking, please transfer AED {amount} "
        f"to the account below and reference **{ref}** in the transfer memo:",
        "",
    ]
    if account.get("bank_name"):
        lines.append(f"Bank: {account['bank_name']}")
    if account.get("account_name"):
        lines.append(f"Account name: {account['account_name']}")
    if account.get("account_number"):
        lines.append(f"Account number: {account['account_number']}")
    if account.get("iban"):
        lines.append(f"IBAN: {account['iban']}")
    if account.get("swift_code"):
        lines.append(f"Swift/BIC: {account['swift_code']}")
    lines.append(f"Reference: {ref}")
    lines.append("")

    upload_url = _proof_upload_url(invoice)
    if upload_url:
        lines.append(f"Once paid, upload a screenshot of the transfer here: {upload_url}")
    else:
        lines.append("Once paid, send a screenshot of the transfer here in this chat "
                      "and our team will confirm it.")
    lines.append("")
    lines.append(
        f"Your slot isn't held until we've verified payment — we'll confirm "
        f"as soon as our team reviews it (usually within a few hours). You "
        f"don't need to keep this chat open; save your reference number "
        f"({ref}) and we'll be in touch."
    )
    return "\n".join(lines)


async def approve_invoice(invoice_id: int, approved_by: str = "") -> dict:
    """Create the appointment, write the calendar event, and notify the
    patient. Deliberately does not touch the in-memory chat session —
    by approval time it has almost certainly expired."""
    from calendar_service import create_appointment_events
    from email_service import send_patient_confirmation, send_clinic_notification

    invoice = db.get_invoice(invoice_id)
    if not invoice:
        return {"status": "not_found"}
    if invoice["status"] == "approved":
        return {"status": "already_approved", "invoice": invoice}

    lead_data = dict(invoice.get("lead", {}))
    lead_data["payment_status"] = "verified"
    lead_data["status"] = "confirmed"

    scheduled_visits = lead_data.get("scheduled_visits") or []

    lead = AppointmentLead(
        session_id=invoice.get("session_id", "unknown"),
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
        payment_status="verified",
        language=lead_data.get("language", "en"),
        source=invoice.get("channel", "website"),
        status="confirmed",
        additional_visits=scheduled_visits[1:] if len(scheduled_visits) > 1 else [],
    )

    appt = db.create_appointment(lead.model_dump() | {"invoice_id": invoice_id})

    lead_dict = lead.model_dump()
    cal_statuses = create_appointment_events(lead_dict)

    # Generate the paid invoice PDF once here and reuse it for both email
    # and WhatsApp — no need to build it twice. Status is explicitly
    # "approved" so the PDF shows the green "PAID & CONFIRMED" badge and
    # the confirmed appointment details, not the pending bank-transfer
    # instructions from before payment was verified.
    pdf_bytes = None
    try:
        import invoice_pdf
        pdf_invoice_data = dict(invoice)
        pdf_invoice_data["status"] = "approved"
        pdf_invoice_data["lead"] = lead_dict
        # BUGFIX: "_account" is a transient, display-only field set only
        # at invoice-creation time (see create_invoice_for_lead) — it's
        # never written to the database, so `invoice` here (freshly
        # fetched via db.get_invoice) never has it. Without this line the
        # approved PDF's "Paid to" bank details table renders completely
        # empty. Looked up by the invoice's persisted bank_account_label
        # so this shows the SAME account the patient actually paid into,
        # not just whichever one happens to be configured as default now.
        pdf_invoice_data["_account"] = _account_by_label(invoice.get("bank_account_label", ""))
        pdf_bytes = invoice_pdf.generate_invoice_pdf(pdf_invoice_data)
    except Exception as e:
        print(f"[Payments] Could not generate paid invoice PDF: {e}")

    patient_email_status = await send_patient_confirmation(
        lead_dict, pdf_bytes=pdf_bytes,
        pdf_filename=f"invoice-{invoice.get('reference', 'nativacare')}.pdf",
    )
    await send_clinic_notification(lead_dict)

    db.update_invoice_status(invoice_id, "approved", appointment_id=appt["id"])

    # Best-effort WhatsApp confirmation if that's how they'd been talking
    # to us, in addition to (not instead of) email. Sends the actual PDF
    # as a WhatsApp document (not just a text confirmation) when we can
    # build a real public link to it — WhatsApp's servers fetch that link
    # themselves, so it only works once FRONTEND_BASE_URL is a genuine
    # public address (e.g. after deploying), not localhost.
    if invoice.get("channel") == "whatsapp" and invoice.get("contact"):
        try:
            from whatsapp_service import send_whatsapp_message, send_whatsapp_document
            confirmation_text = (
                f"Payment verified — your {lead.service_name} appointment on "
                f"{lead.appointment_date} at {lead.appointment_time} is confirmed. "
                f"See you then!"
            )
            reference = invoice.get("reference", "")
            pdf_link = f"{FRONTEND_BASE_URL}/invoices/by-reference/{reference}/pdf" if FRONTEND_BASE_URL and reference else ""
            if pdf_link:
                await send_whatsapp_document(
                    invoice["contact"], pdf_link, f"invoice-{reference}.pdf",
                    caption=confirmation_text,
                )
            else:
                await send_whatsapp_message(invoice["contact"], confirmation_text)
        except Exception as e:
            print(f"[Payments] WhatsApp confirmation failed: {e}")

    return {
        "status": "approved",
        "appointment": appt,
        "calendar": cal_statuses,
        "patient_email": patient_email_status,
    }


async def reject_invoice(invoice_id: int, reason: str = "", rejected_by: str = "") -> dict:
    invoice = db.get_invoice(invoice_id)
    if not invoice:
        return {"status": "not_found"}

    db.update_invoice_status(invoice_id, "rejected", reject_reason=reason)

    msg = ("We couldn't verify the payment screenshot you sent"
           + (f" ({reason})" if reason else "")
           + f". Please reply with a clearer screenshot, or contact us directly — "
             f"your reference number is {invoice['reference']}.")

    if invoice.get("channel") == "whatsapp" and invoice.get("contact"):
        try:
            from whatsapp_service import send_whatsapp_message
            await send_whatsapp_message(invoice["contact"], msg)
        except Exception as e:
            print(f"[Payments] WhatsApp rejection notice failed: {e}")

    return {"status": "rejected", "message": msg}
