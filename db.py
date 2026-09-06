import os
import json
import secrets
from datetime import datetime, timezone

from sqlalchemy import (
    create_engine, Column, Integer, String, Text, Boolean, DateTime,
)
from sqlalchemy.orm import declarative_base, sessionmaker

DB_PATH = os.getenv("NATIVACARE_DB_PATH", "nativacare.db")
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class Appointment(Base):
    __tablename__ = "appointments"
    id = Column(Integer, primary_key=True)
    session_id = Column(String, default="")
    patient_name = Column(String, default="")
    email = Column(String, default="")
    phone = Column(String, default="")
    service_id = Column(String, default="")
    service_name = Column(String, default="")
    midwife_id = Column(String, default="")
    midwife_name = Column(String, default="")
    location_type = Column(String, default="")
    patient_address = Column(String, default="")
    appointment_date = Column(String, default="")
    appointment_time = Column(String, default="")
    duration_minutes = Column(Integer, default=60)
    price_aed = Column(String, default="")
    package_id = Column(String, default="")
    language = Column(String, default="en")
    source = Column(String, default="website")
    status = Column(String, default="pending")
    notes = Column(String, default="")
    payment_status = Column(String, default="")
    invoice_id = Column(Integer, default=0)
    additional_visits_json = Column(Text, default="[]")
    # JSON-encoded list of {date, time, midwife_id, midwife_name} for
    # visits 2..N of a multi-visit program booking (see models.py's
    # AppointmentLead.additional_visits). Empty list ("[]") for an
    # ordinary single-visit booking.
    created_at = Column(String, default=_now)


class Invoice(Base):
    __tablename__ = "invoices"
    id = Column(Integer, primary_key=True)
    reference = Column(String, unique=True, index=True)
    session_id = Column(String, default="")
    channel = Column(String, default="website")
    contact = Column(String, default="")
    patient_name = Column(String, default="")
    amount_aed = Column(String, default="")
    bank_account_label = Column(String, default="")
    lead_json = Column(Text, default="{}")
    status = Column(String, default="pending_proof")
    reject_reason = Column(String, default="")
    appointment_id = Column(Integer, default=0)
    action_token = Column(String, default="", index=True)
    # Unguessable per-invoice secret, separate from `reference` (shown to
    # the patient). This is the credential behind the one-click
    # Approve/Reject links sent in the staff notification email — it
    # lets those links work without also requiring the dashboard's
    # X-Admin-Key, since the token itself only ever reaches staff.
    created_at = Column(String, default=_now)
    updated_at = Column(String, default=_now)


class PaymentProof(Base):
    __tablename__ = "payment_proofs"
    id = Column(Integer, primary_key=True)
    invoice_id = Column(Integer, index=True)
    file_path = Column(String, default="")
    source = Column(String, default="website")
    created_at = Column(String, default=_now)


class Conversation(Base):
    __tablename__ = "conversations"
    session_id = Column(String, primary_key=True)
    human_mode = Column(Boolean, default=False)
    channel = Column(String, default="")
    contact = Column(String, default="")
    updated_at = Column(String, default=_now)


def init_db():
    Base.metadata.create_all(engine)
    _migrate_add_missing_columns()


def _migrate_add_missing_columns():
    """Lightweight auto-migration: Base.metadata.create_all() only creates
    tables that don't exist yet — it never adds new columns to a table
    that's already there. Since `action_token` was added to Invoice (and
    `additional_visits_json` to Appointment) after people may already have
    a real nativacare.db on disk, this adds them (and anything similar
    added later) without anyone needing to delete their existing data."""
    from sqlalchemy import inspect, text
    inspector = inspect(engine)
    if "invoices" in inspector.get_table_names():
        existing_cols = {c["name"] for c in inspector.get_columns("invoices")}
        if "action_token" not in existing_cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE invoices ADD COLUMN action_token VARCHAR DEFAULT ''"))
            print("[db] Migrated: added action_token column to invoices table.")

    if "appointments" in inspector.get_table_names():
        existing_cols = {c["name"] for c in inspector.get_columns("appointments")}
        if "additional_visits_json" not in existing_cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE appointments ADD COLUMN additional_visits_json TEXT DEFAULT '[]'"))
            print("[db] Migrated: added additional_visits_json column to appointments table.")


def _appt_to_dict(a):
    d = {c.name: getattr(a, c.name) for c in a.__table__.columns}
    try:
        d["additional_visits"] = json.loads(d.get("additional_visits_json") or "[]")
    except (TypeError, ValueError):
        d["additional_visits"] = []
    return d


def create_appointment(lead: dict) -> dict:
    db = SessionLocal()
    try:
        a = Appointment(
            session_id=lead.get("session_id", ""),
            patient_name=lead.get("patient_name", ""),
            email=lead.get("email", ""),
            phone=lead.get("phone", ""),
            service_id=lead.get("service_id", ""),
            service_name=lead.get("service_name", ""),
            midwife_id=lead.get("midwife_id", ""),
            midwife_name=lead.get("midwife_name", ""),
            location_type=lead.get("location_type", ""),
            patient_address=lead.get("patient_address", ""),
            appointment_date=lead.get("appointment_date", ""),
            appointment_time=lead.get("appointment_time", ""),
            duration_minutes=int(lead.get("duration_minutes") or 60),
            price_aed=str(lead.get("price_aed") or ""),
            package_id=lead.get("package_id", ""),
            language=lead.get("language", "en"),
            source=lead.get("source", "website"),
            status=lead.get("status", "pending"),
            notes=lead.get("notes", ""),
            payment_status=lead.get("payment_status", ""),
            invoice_id=int(lead.get("invoice_id") or 0),
            additional_visits_json=json.dumps(lead.get("additional_visits") or []),
        )
        db.add(a)
        db.commit()
        db.refresh(a)
        return _appt_to_dict(a)
    finally:
        db.close()


def list_appointments() -> list:
    db = SessionLocal()
    try:
        rows = db.query(Appointment).order_by(Appointment.id.desc()).all()
        return [_appt_to_dict(a) for a in rows]
    finally:
        db.close()


def list_appointments_for(midwife_id: str, date: str) -> list:
    db = SessionLocal()
    try:
        rows = (
            db.query(Appointment)
            .filter(
                Appointment.midwife_id == midwife_id,
                Appointment.appointment_date == date,
                Appointment.status.notin_(["cancelled", "rejected"]),
            )
            .all()
        )
        return [_appt_to_dict(a) for a in rows]
    finally:
        db.close()


def update_appointment_status(appointment_id: int, status: str):
    db = SessionLocal()
    try:
        a = db.query(Appointment).filter(Appointment.id == appointment_id).first()
        if a:
            a.status = status
            db.commit()
    finally:
        db.close()


def _inv_to_dict(i):
    d = {c.name: getattr(i, c.name) for c in i.__table__.columns}
    try:
        d["lead"] = json.loads(d.get("lead_json") or "{}")
    except (TypeError, ValueError):
        d["lead"] = {}
    return d


def _generate_reference() -> str:
    return "NC-" + secrets.token_hex(4).upper()


def create_invoice(session_id: str, channel: str, contact: str,
                    lead: dict, amount_aed: str,
                    bank_account_label: str) -> dict:
    db = SessionLocal()
    try:
        ref = _generate_reference()
        while db.query(Invoice).filter(Invoice.reference == ref).first():
            ref = _generate_reference()
        inv = Invoice(
            reference=ref,
            session_id=session_id,
            channel=channel,
            contact=contact,
            patient_name=lead.get("patient_name", ""),
            amount_aed=str(amount_aed),
            bank_account_label=bank_account_label,
            lead_json=json.dumps(lead),
            status="pending_proof",
            action_token=secrets.token_urlsafe(24),
        )
        db.add(inv)
        db.commit()
        db.refresh(inv)
        return _inv_to_dict(inv)
    finally:
        db.close()


def get_invoice(invoice_id: int):
    db = SessionLocal()
    try:
        inv = db.query(Invoice).filter(Invoice.id == invoice_id).first()
        return _inv_to_dict(inv) if inv else None
    finally:
        db.close()


def get_invoice_by_reference(reference: str):
    db = SessionLocal()
    try:
        inv = db.query(Invoice).filter(Invoice.reference == reference).first()
        return _inv_to_dict(inv) if inv else None
    finally:
        db.close()


def list_invoices(status: str = None) -> list:
    db = SessionLocal()
    try:
        q = db.query(Invoice)
        if status:
            q = q.filter(Invoice.status == status)
        rows = q.order_by(Invoice.id.desc()).all()
        return [_inv_to_dict(i) for i in rows]
    finally:
        db.close()


def update_invoice_status(invoice_id: int, status: str, **fields):
    db = SessionLocal()
    try:
        inv = db.query(Invoice).filter(Invoice.id == invoice_id).first()
        if not inv:
            return None
        inv.status = status
        inv.updated_at = _now()
        for k, v in fields.items():
            if hasattr(inv, k):
                setattr(inv, k, v)
        db.commit()
        db.refresh(inv)
        return _inv_to_dict(inv)
    finally:
        db.close()


def add_payment_proof(invoice_id: int, file_path: str, source: str) -> dict:
    db = SessionLocal()
    try:
        p = PaymentProof(invoice_id=invoice_id, file_path=file_path, source=source)
        db.add(p)
        db.commit()
        db.refresh(p)
        return {c.name: getattr(p, c.name) for c in p.__table__.columns}
    finally:
        db.close()


def list_proofs_for_invoice(invoice_id: int) -> list:
    db = SessionLocal()
    try:
        rows = (
            db.query(PaymentProof)
            .filter(PaymentProof.invoice_id == invoice_id)
            .order_by(PaymentProof.id.desc())
            .all()
        )
        return [{c.name: getattr(p, c.name) for c in p.__table__.columns} for p in rows]
    finally:
        db.close()


def set_human_mode(session_id: str, enabled: bool, channel: str = "", contact: str = ""):
    db = SessionLocal()
    try:
        c = db.query(Conversation).filter(Conversation.session_id == session_id).first()
        if not c:
            c = Conversation(session_id=session_id)
            db.add(c)
        c.human_mode = enabled
        c.updated_at = _now()
        if channel:
            c.channel = channel
        if contact:
            c.contact = contact
        db.commit()
    finally:
        db.close()


def get_conversation(session_id: str):
    db = SessionLocal()
    try:
        c = db.query(Conversation).filter(Conversation.session_id == session_id).first()
        if not c:
            return None
        return {col.name: getattr(c, col.name) for col in c.__table__.columns}
    finally:
        db.close()


def list_human_mode_conversations() -> list:
    db = SessionLocal()
    try:
        rows = db.query(Conversation).filter(Conversation.human_mode == True).all()  # noqa: E712
        return [{col.name: getattr(c, col.name) for col in c.__table__.columns} for c in rows]
    finally:
        db.close()
