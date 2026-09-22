"""NativaCare operational DB compatibility layer.

Production: set DATABASE_URL (Railway PostgreSQL) and all durable records go
there. Development without DATABASE_URL keeps the legacy SQLite implementation.
"""
import os
if os.getenv("DATABASE_URL","").strip():
    from persistent_store import *  # noqa: F401,F403
else:
    # SQLite fallback intentionally kept small but API-compatible.
    import json, secrets
    from datetime import datetime, timezone
    from sqlalchemy import create_engine, Column, Integer, String, Text, Boolean
    from sqlalchemy.orm import declarative_base, sessionmaker
    DB_PATH=os.getenv("NATIVACARE_DB_PATH","nativacare.db")
    engine=create_engine(f"sqlite:///{DB_PATH}",connect_args={"check_same_thread":False});SessionLocal=sessionmaker(bind=engine);Base=declarative_base()
    def _now():return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    class Appointment(Base):
        __tablename__="appointments";id=Column(Integer,primary_key=True);session_id=Column(String,default="");patient_name=Column(String,default="");email=Column(String,default="");phone=Column(String,default="");service_id=Column(String,default="");service_name=Column(String,default="");midwife_id=Column(String,default="");midwife_name=Column(String,default="");location_type=Column(String,default="");patient_address=Column(String,default="");appointment_date=Column(String,default="");appointment_time=Column(String,default="");duration_minutes=Column(Integer,default=60);price_aed=Column(String,default="");package_id=Column(String,default="");language=Column(String,default="en");source=Column(String,default="website");status=Column(String,default="pending");notes=Column(Text,default="");payment_status=Column(String,default="");invoice_id=Column(Integer,default=0);additional_visits_json=Column(Text,default="[]");created_at=Column(String,default=_now)
    class Invoice(Base):
        __tablename__="invoices";id=Column(Integer,primary_key=True);reference=Column(String,unique=True,index=True);session_id=Column(String,default="");channel=Column(String,default="website");contact=Column(String,default="");patient_name=Column(String,default="");amount_aed=Column(String,default="");bank_account_label=Column(String,default="");lead_json=Column(Text,default="{}");status=Column(String,default="pending_proof");reject_reason=Column(String,default="");appointment_id=Column(Integer,default=0);action_token=Column(String,default="",index=True);created_at=Column(String,default=_now);updated_at=Column(String,default=_now)
    class PaymentProof(Base):
        __tablename__="payment_proofs";id=Column(Integer,primary_key=True);invoice_id=Column(Integer,index=True);file_path=Column(String,default="");source=Column(String,default="website");created_at=Column(String,default=_now)
    class Conversation(Base):
        __tablename__="conversations";session_id=Column(String,primary_key=True);human_mode=Column(Boolean,default=False);channel=Column(String,default="");contact=Column(String,default="");updated_at=Column(String,default=_now)
    def init_db():Base.metadata.create_all(engine)
    def _d(x):return {c.name:getattr(x,c.name) for c in x.__table__.columns}
    def _a(x):
        d=_d(x)
        try:d["additional_visits"]=json.loads(d.get("additional_visits_json") or "[]")
        except:d["additional_visits"]=[]
        return d
    def _i(x):
        d=_d(x)
        try:d["lead"]=json.loads(d.get("lead_json") or "{}")
        except:d["lead"]={}
        return d
    def create_appointment(lead):
        s=SessionLocal();cols={c.name for c in Appointment.__table__.columns}-{ "id","created_at"};data={k:v for k,v in lead.items() if k in cols};data["duration_minutes"]=int(data.get("duration_minutes") or 60);data["invoice_id"]=int(data.get("invoice_id") or 0);data["additional_visits_json"]=json.dumps(lead.get("additional_visits") or []);x=Appointment(**data);s.add(x);s.commit();s.refresh(x);d=_a(x);s.close();return d
    def list_appointments():
        s=SessionLocal();r=[_a(x) for x in s.query(Appointment).order_by(Appointment.id.desc()).all()];s.close();return r
    def list_appointments_for(midwife_id,date):
        s=SessionLocal();r=[_a(x) for x in s.query(Appointment).filter(Appointment.midwife_id==midwife_id,Appointment.appointment_date==date,Appointment.status.notin_(["cancelled","rejected"])).all()];s.close();return r
    def update_appointment_status(i,status):
        s=SessionLocal();x=s.query(Appointment).filter(Appointment.id==i).first();
        if x:x.status=status;s.commit()
        s.close()
    def create_invoice(session_id,channel,contact,lead,amount_aed,bank_account_label):
        s=SessionLocal();ref="NC-"+secrets.token_hex(4).upper();x=Invoice(reference=ref,session_id=session_id,channel=channel,contact=contact,patient_name=lead.get("patient_name",""),amount_aed=str(amount_aed),bank_account_label=bank_account_label,lead_json=json.dumps(lead),action_token=secrets.token_urlsafe(24));s.add(x);s.commit();s.refresh(x);d=_i(x);s.close();return d
    def get_invoice(i):
        s=SessionLocal();x=s.query(Invoice).filter(Invoice.id==i).first();d=_i(x) if x else None;s.close();return d
    def get_invoice_by_reference(r):
        s=SessionLocal();x=s.query(Invoice).filter(Invoice.reference==r).first();d=_i(x) if x else None;s.close();return d
    def list_invoices(status=None):
        s=SessionLocal();q=s.query(Invoice);q=q.filter(Invoice.status==status) if status else q;r=[_i(x) for x in q.order_by(Invoice.id.desc()).all()];s.close();return r
    def update_invoice_status(i,status,**fields):
        s=SessionLocal();x=s.query(Invoice).filter(Invoice.id==i).first()
        if not x:s.close();return None
        x.status=status;x.updated_at=_now()
        for k,v in fields.items():
            if hasattr(x,k):setattr(x,k,v)
        s.commit();s.refresh(x);d=_i(x);s.close();return d
    def add_payment_proof(i,file_path,source):
        s=SessionLocal();x=PaymentProof(invoice_id=i,file_path=file_path,source=source);s.add(x);s.commit();s.refresh(x);d=_d(x);s.close();return d
    def list_proofs_for_invoice(i):
        s=SessionLocal();r=[_d(x) for x in s.query(PaymentProof).filter(PaymentProof.invoice_id==i).order_by(PaymentProof.id.desc()).all()];s.close();return r
    def set_human_mode(session_id,enabled,channel="",contact=""):
        s=SessionLocal();x=s.query(Conversation).filter(Conversation.session_id==session_id).first()
        if not x:x=Conversation(session_id=session_id);s.add(x)
        x.human_mode=enabled;x.updated_at=_now();x.channel=channel or x.channel;x.contact=contact or x.contact;s.commit();s.close()
    def get_conversation(session_id):
        s=SessionLocal();x=s.query(Conversation).filter(Conversation.session_id==session_id).first();d=_d(x) if x else None;s.close();return d
    def list_human_mode_conversations():
        s=SessionLocal();r=[_d(x) for x in s.query(Conversation).filter(Conversation.human_mode==True).all()];s.close();return r
    init_db()
