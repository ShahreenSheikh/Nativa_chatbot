"""Durable NativaCare storage.

When DATABASE_URL is set (recommended: Railway PostgreSQL), this module
stores appointments, invoices, payment-proof metadata, conversation takeover
state, complete chat logs, and chatbot session/context in PostgreSQL.
Nothing important depends on the Railway app container filesystem.
"""
import os, json, secrets
from datetime import datetime, timezone
from sqlalchemy import create_engine, Column, Integer, String, Text, Boolean
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
ENABLED = bool(DATABASE_URL)
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

Base = declarative_base()
engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=300) if ENABLED else None
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False) if ENABLED else None

def _now(): return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

class Appointment(Base):
    __tablename__="appointments"
    id=Column(Integer,primary_key=True); session_id=Column(String,default=""); patient_name=Column(String,default=""); email=Column(String,default=""); phone=Column(String,default=""); service_id=Column(String,default=""); service_name=Column(String,default=""); midwife_id=Column(String,default=""); midwife_name=Column(String,default=""); location_type=Column(String,default=""); patient_address=Column(String,default=""); appointment_date=Column(String,default=""); appointment_time=Column(String,default=""); duration_minutes=Column(Integer,default=60); price_aed=Column(String,default=""); package_id=Column(String,default=""); language=Column(String,default="en"); source=Column(String,default="website"); status=Column(String,default="pending"); notes=Column(Text,default=""); payment_status=Column(String,default=""); invoice_id=Column(Integer,default=0); additional_visits_json=Column(Text,default="[]"); created_at=Column(String,default=_now)
class Invoice(Base):
    __tablename__="invoices"
    id=Column(Integer,primary_key=True); reference=Column(String,unique=True,index=True); session_id=Column(String,default=""); channel=Column(String,default="website"); contact=Column(String,default=""); patient_name=Column(String,default=""); amount_aed=Column(String,default=""); bank_account_label=Column(String,default=""); lead_json=Column(Text,default="{}"); status=Column(String,default="pending_proof"); reject_reason=Column(String,default=""); appointment_id=Column(Integer,default=0); action_token=Column(String,default="",index=True); created_at=Column(String,default=_now); updated_at=Column(String,default=_now)
class PaymentProof(Base):
    __tablename__="payment_proofs"
    id=Column(Integer,primary_key=True); invoice_id=Column(Integer,index=True); file_path=Column(String,default=""); source=Column(String,default="website"); created_at=Column(String,default=_now)
class Conversation(Base):
    __tablename__="conversations"
    session_id=Column(String,primary_key=True); human_mode=Column(Boolean,default=False); channel=Column(String,default=""); contact=Column(String,default=""); updated_at=Column(String,default=_now)
class ChatLog(Base):
    __tablename__="chat_logs"
    id=Column(Integer,primary_key=True,autoincrement=True); timestamp=Column(String,default=_now,index=True); session_id=Column(String,index=True); user_message=Column(Text,default=""); ai_response=Column(Text,default="")
class ChatSession(Base):
    __tablename__="chat_sessions"
    session_id=Column(String,primary_key=True); state_json=Column(Text,default="{}"); updated_at=Column(String,default=_now,index=True)

def init_db():
    if ENABLED: Base.metadata.create_all(engine)

def _dict(row): return {c.name:getattr(row,c.name) for c in row.__table__.columns}
def _appt(row):
    d=_dict(row)
    try:d["additional_visits"]=json.loads(d.get("additional_visits_json") or "[]")
    except Exception:d["additional_visits"]=[]
    return d
def _inv(row):
    d=_dict(row)
    try:d["lead"]=json.loads(d.get("lead_json") or "{}")
    except Exception:d["lead"]={}
    return d

def create_appointment(lead):
    with SessionLocal() as s:
        cols={c.name for c in Appointment.__table__.columns}-{ "id","created_at" }
        data={k:v for k,v in lead.items() if k in cols}
        data["duration_minutes"]=int(data.get("duration_minutes") or 60); data["invoice_id"]=int(data.get("invoice_id") or 0); data["additional_visits_json"]=json.dumps(lead.get("additional_visits") or [])
        a=Appointment(**data); s.add(a); s.commit(); s.refresh(a); return _appt(a)
def list_appointments():
    with SessionLocal() as s:return [_appt(x) for x in s.query(Appointment).order_by(Appointment.id.desc()).all()]
def list_appointments_for(midwife_id,date):
    with SessionLocal() as s:return [_appt(x) for x in s.query(Appointment).filter(Appointment.midwife_id==midwife_id,Appointment.appointment_date==date,Appointment.status.notin_(["cancelled","rejected"])).all()]
def update_appointment_status(appointment_id,status):
    with SessionLocal() as s:
        a=s.query(Appointment).filter(Appointment.id==appointment_id).first()
        if a:a.status=status;s.commit()
def create_invoice(session_id,channel,contact,lead,amount_aed,bank_account_label):
    with SessionLocal() as s:
        ref="NC-"+secrets.token_hex(4).upper()
        while s.query(Invoice).filter(Invoice.reference==ref).first():ref="NC-"+secrets.token_hex(4).upper()
        i=Invoice(reference=ref,session_id=session_id,channel=channel,contact=contact,patient_name=lead.get("patient_name",""),amount_aed=str(amount_aed),bank_account_label=bank_account_label,lead_json=json.dumps(lead),status="pending_proof",action_token=secrets.token_urlsafe(24));s.add(i);s.commit();s.refresh(i);return _inv(i)
def get_invoice(invoice_id):
    with SessionLocal() as s:
        x=s.query(Invoice).filter(Invoice.id==invoice_id).first();return _inv(x) if x else None
def get_invoice_by_reference(reference):
    with SessionLocal() as s:
        x=s.query(Invoice).filter(Invoice.reference==reference).first();return _inv(x) if x else None
def list_invoices(status=None):
    with SessionLocal() as s:
        q=s.query(Invoice)
        if status:q=q.filter(Invoice.status==status)
        return [_inv(x) for x in q.order_by(Invoice.id.desc()).all()]
def update_invoice_status(invoice_id,status,**fields):
    with SessionLocal() as s:
        x=s.query(Invoice).filter(Invoice.id==invoice_id).first()
        if not x:return None
        x.status=status;x.updated_at=_now()
        for k,v in fields.items():
            if hasattr(x,k):setattr(x,k,v)
        s.commit();s.refresh(x);return _inv(x)
def add_payment_proof(invoice_id,file_path,source):
    with SessionLocal() as s:
        x=PaymentProof(invoice_id=invoice_id,file_path=file_path,source=source);s.add(x);s.commit();s.refresh(x);return _dict(x)
def list_proofs_for_invoice(invoice_id):
    with SessionLocal() as s:return [_dict(x) for x in s.query(PaymentProof).filter(PaymentProof.invoice_id==invoice_id).order_by(PaymentProof.id.desc()).all()]
def set_human_mode(session_id,enabled,channel="",contact=""):
    with SessionLocal() as s:
        x=s.query(Conversation).filter(Conversation.session_id==session_id).first()
        if not x:x=Conversation(session_id=session_id);s.add(x)
        x.human_mode=enabled;x.updated_at=_now();x.channel=channel or x.channel;x.contact=contact or x.contact;s.commit()
def get_conversation(session_id):
    with SessionLocal() as s:
        x=s.query(Conversation).filter(Conversation.session_id==session_id).first();return _dict(x) if x else None
def list_human_mode_conversations():
    with SessionLocal() as s:return [_dict(x) for x in s.query(Conversation).filter(Conversation.human_mode==True).all()]
def save_chat_log(session_id,user_message,ai_response):
    with SessionLocal() as s:
        x=ChatLog(session_id=session_id,user_message=user_message,ai_response=ai_response);s.add(x);s.commit();s.refresh(x);return x.id
def get_chat_logs(limit=100,offset=0):
    with SessionLocal() as s:
        rows=s.query(ChatLog).order_by(ChatLog.id.desc()).offset(offset).limit(limit).all();return [_dict(x) for x in reversed(rows)]
def get_chat_logs_for_session(session_id,after_id=0,limit=500):
    with SessionLocal() as s:return [_dict(x) for x in s.query(ChatLog).filter(ChatLog.session_id==session_id,ChatLog.id>after_id).order_by(ChatLog.id.asc()).limit(limit).all()]
def save_session(session_id,state):
    def clean(v):
        if isinstance(v,datetime):return {"__datetime__":v.isoformat()}
        if isinstance(v,dict):return {k:clean(x) for k,x in v.items() if k!="_cache"}
        if isinstance(v,list):return [clean(x) for x in v]
        return v
    payload=json.dumps(clean(state),ensure_ascii=False)
    with SessionLocal() as s:
        x=s.query(ChatSession).filter(ChatSession.session_id==session_id).first()
        if not x:x=ChatSession(session_id=session_id);s.add(x)
        x.state_json=payload;x.updated_at=_now();s.commit()
def load_session(session_id):
    with SessionLocal() as s:
        x=s.query(ChatSession).filter(ChatSession.session_id==session_id).first()
        if not x:return None
        try:data=json.loads(x.state_json or "{}")
        except Exception:return None
    def restore(v):
        if isinstance(v,dict) and set(v)=={"__datetime__"}:
            try:return datetime.fromisoformat(v["__datetime__"])
            except Exception:return datetime.utcnow()
        if isinstance(v,dict):return {k:restore(x) for k,x in v.items()}
        if isinstance(v,list):return [restore(x) for x in v]
        return v
    return restore(data)

if ENABLED:
    init_db(); print("[Persistence] PostgreSQL enabled via DATABASE_URL — patient data, conversations and sessions are durable.")
