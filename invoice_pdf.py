"""
PDF invoice generator for NativaCare — completely free and self-hosted.

Uses fpdf2 (pure Python, no system dependencies like wkhtmltopdf or
Cairo/Pango that weasyprint needs) — this matters for deployment: it
installs cleanly on Railway's default Python buildpack with nothing
extra, and there's no per-document fee or API key like a hosted PDF
service (DocRaptor, PDFShift, etc.) would require. Pure computation, no
external network calls.

Public API:
    generate_invoice_pdf(invoice: dict) -> bytes
"""

import os
from pathlib import Path
from fpdf import FPDF

CLINIC_NAME = os.getenv("CLINIC_NAME", "NativaCare")
CLINIC_PHONE = os.getenv("CLINIC_PHONE", "+971507297197")
CLINIC_EMAIL = os.getenv("CLINIC_EMAIL", "")

# Real logo, downloaded from nativacare.com and converted to PNG (fpdf2's
# image support is more reliably tested against PNG/JPEG than WEBP across
# environments). Falls back to plain text if the file isn't there for some
# reason — never crashes invoice generation over a missing logo.
_LOGO_PATH = Path(__file__).parent / "assets" / "nativacare_logo.png"

# Brand colors, matching the dashboard's own palette so the PDF doesn't
# look like a completely different product.
_BROWN = (118, 90, 20)
_SAGE = (85, 99, 73)
_GREY = (110, 110, 110)


def _status_label(status: str) -> tuple:
    """Returns (label, rgb color) for the status badge."""
    return {
        "pending_proof": ("AWAITING PAYMENT", (150, 130, 30)),
        "submitted": ("UNDER REVIEW", (150, 130, 30)),
        "approved": ("PAID & CONFIRMED", (62, 75, 51)),
        "rejected": ("PAYMENT REJECTED", (147, 0, 10)),
    }.get(status, (status.upper(), _GREY))


def generate_invoice_pdf(invoice: dict) -> bytes:
    """Build a one-page PDF invoice for a single booking invoice record
    (the dict shape returned by db.get_invoice() / db.get_invoice_by_reference()).
    Returns raw PDF bytes — caller decides whether to serve it directly,
    email it as an attachment, or save it to disk."""
    lead = invoice.get("lead", {}) or {}
    reference = invoice.get("reference", "")
    amount = invoice.get("amount_aed", "")
    status = invoice.get("status", "")
    created_at = (invoice.get("created_at") or "")[:10]  # just the date part

    pdf = FPDF(unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()
    pdf.set_margins(20, 20, 20)

    # --- Header ---
    if _LOGO_PATH.exists():
        # Real aspect ratio is 618x152 (~4.07:1) — 55mm wide keeps it
        # readable without dominating the page.
        pdf.image(str(_LOGO_PATH), x=20, y=18, w=55)
        pdf.set_xy(20, 34)
    else:
        pdf.set_font("Helvetica", "B", 22)
        pdf.set_text_color(*_BROWN)
        pdf.cell(0, 12, CLINIC_NAME, ln=True)

    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*_GREY)
    contact_line = " · ".join(p for p in [CLINIC_PHONE, CLINIC_EMAIL] if p)
    if contact_line:
        pdf.cell(0, 6, contact_line, ln=True)
    pdf.ln(4)

    # --- Title + status badge ---
    pdf.set_font("Helvetica", "B", 16)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(100, 10, "INVOICE", ln=False)

    label, color = _status_label(status)
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(*color)
    pdf.cell(0, 10, label, align="R", ln=True)
    pdf.ln(2)

    # --- Reference / date row ---
    pdf.set_draw_color(220, 220, 220)
    pdf.line(20, pdf.get_y(), 190, pdf.get_y())
    pdf.ln(4)

    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(60, 60, 60)
    pdf.cell(95, 7, f"Reference: {reference}", ln=False)
    pdf.cell(0, 7, f"Date: {created_at}", align="R", ln=True)
    pdf.ln(6)

    # --- Bill to ---
    pdf.set_font("Helvetica", "B", 11)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(0, 7, "Billed to", ln=True)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(60, 60, 60)
    pdf.cell(0, 6, lead.get("patient_name") or "(name not provided)", ln=True)
    if lead.get("phone"):
        pdf.cell(0, 6, lead["phone"], ln=True)
    if lead.get("email"):
        pdf.cell(0, 6, lead["email"], ln=True)
    pdf.ln(6)

    # --- Line item table ---
    pdf.set_fill_color(*_BROWN)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(110, 9, "  Service", border=0, fill=True)
    pdf.cell(40, 9, "Duration", border=0, fill=True, align="C")
    pdf.cell(30, 9, "Amount (AED)", border=0, fill=True, align="R")
    pdf.ln(9)

    pdf.set_text_color(40, 40, 40)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_fill_color(248, 247, 245)
    duration = lead.get("duration_minutes")
    duration_str = f"{duration} min" if duration else "-"
    pdf.cell(110, 10, f"  {lead.get('service_name') or 'Service'}", border=0, fill=True)
    pdf.cell(40, 10, duration_str, border=0, fill=True, align="C")
    pdf.cell(30, 10, str(amount) if amount else "-", border=0, fill=True, align="R")
    pdf.ln(10)
    pdf.ln(4)

    # --- Total ---
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(30, 30, 30)
    pdf.cell(150, 9, "Total due", align="R")
    pdf.cell(30, 9, f"AED {amount}" if amount else "-", align="R", ln=True)
    pdf.ln(8)

    account = invoice.get("_account") or {}

    def _draw_bank_details_table(reference_label="Reference"):
        """Shared bank-details block — used both while payment is
        pending (as instructions) and after approval (as a permanent
        record of which account was paid, useful for the client's own
        expense/reimbursement records)."""
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(60, 60, 60)
        rows = [
            ("Bank", account.get("bank_name")),
            ("Account name", account.get("account_name")),
            ("Account number", account.get("account_number")),
            ("IBAN", account.get("iban")),
            ("Swift/BIC", account.get("swift_code")),
            (reference_label, reference),
        ]
        for label_txt, value in rows:
            if value:
                pdf.cell(60, 6, label_txt, ln=False)
                pdf.cell(0, 6, str(value), ln=True)

    if status in ("pending_proof", "submitted"):
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(0, 8, "Bank transfer details", ln=True)
        _draw_bank_details_table(reference_label="Reference (put this in the memo)")
        pdf.ln(4)
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(*_GREY)
        pdf.multi_cell(0, 5,
            "This slot is not held until payment has been verified by our "
            "team. Please upload a screenshot of your transfer once complete.")
    elif status == "approved":
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(*_SAGE)
        appt_date = lead.get("appointment_date", "")
        appt_time = lead.get("appointment_time", "")
        midwife = lead.get("midwife_name", "")
        if appt_date:
            pdf.cell(0, 6, f"Appointment confirmed: {appt_date} at {appt_time}"
                           + (f" with {midwife}" if midwife else ""), ln=True)
        pdf.ln(6)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(30, 30, 30)
        pdf.cell(0, 8, "Paid to", ln=True)
        _draw_bank_details_table()
    elif status == "rejected":
        reason = invoice.get("reject_reason", "")
        pdf.set_font("Helvetica", "", 10)
        pdf.set_text_color(147, 0, 10)
        pdf.multi_cell(0, 6, "This payment could not be verified"
                             + (f": {reason}" if reason else ".")
                             + " Please contact us or resubmit a clearer screenshot.")

    # --- Footer ---
    # Disable auto-page-break just for this cell — with the auto-break
    # margin at 20mm, set_y(-25) plus this cell's height triggers fpdf2's
    # "not enough room" check by about 1mm, silently creating a blank
    # second page. The footer text is short and never needs to wrap, so
    # there's no actual risk of content being cut off by turning this off
    # right before drawing it.
    pdf.set_auto_page_break(False)
    pdf.set_y(-25)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(*_GREY)
    pdf.cell(0, 6, f"{CLINIC_NAME} - generated automatically, no signature required.", align="C")

    return bytes(pdf.output())
