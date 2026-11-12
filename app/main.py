from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests
from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, UniqueConstraint, create_engine, inspect, select, update, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./clinic.db")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

class Slot(Base):
    __tablename__ = "slots"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    duration_minutes: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    is_booked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)

class PatientAccount(Base):
    __tablename__ = "patient_accounts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    full_name: Mapped[str] = mapped_column(String(120), nullable=False)
    date_of_birth: Mapped[date] = mapped_column(Date, nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

class Booking(Base):
    __tablename__ = "bookings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    slot_id: Mapped[str] = mapped_column(ForeignKey("slots.id"), nullable=False, index=True)
    patient_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    patient_name: Mapped[str] = mapped_column(String(120), nullable=False)
    patient_email: Mapped[str] = mapped_column(String(254), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(20), default="confirmed", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False)

class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    patient_id: Mapped[str] = mapped_column(String(80), nullable=False)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    booking_id: Mapped[str] = mapped_column(String(36), nullable=False)
    __table_args__ = (UniqueConstraint("patient_id", "key", name="uq_idempotency_patient_key"),)

Base.metadata.create_all(engine)
# Forward migration for existing Render databases.
if "reason" not in {column["name"] for column in inspect(engine).get_columns("bookings")}:
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE bookings ADD COLUMN reason VARCHAR(1000) NOT NULL DEFAULT ''"))

app = FastAPI(title="Clinic Health Booking API", version="1.0.0", description="Clinic appointment booking API with concurrency safety and idempotency.")

@app.on_event("startup")
def ensure_demo_slots_exist():
    """Keep a rolling 30-day supply of weekday demo slots for patient self-booking."""
    with SessionLocal() as db:
        now = datetime.now(timezone.utc)
        today = now.date()
        for day_offset in range(30):
            target = today + timedelta(days=day_offset)
            if target.weekday() >= 5:
                continue
            for hour in range(9, 17):
                starts = datetime.combine(target, dtime(hour, 0), tzinfo=timezone.utc)
                if starts <= now:
                    continue
                exists = db.scalar(select(Slot.id).where(Slot.starts_at == starts))
                if not exists:
                    db.add(Slot(starts_at=starts, duration_minutes=30))
        db.commit()

app.mount("/static", StaticFiles(directory="frontend"), name="static")

# Small in-process limiter. For multiple replicas, replace with a shared Redis-backed limiter.
_rate_lock = Lock()
_rate_hits: dict[str, list[float]] = {}
def rate_limit(request: Request):
    now = time.monotonic()
    key = request.client.host if request.client else "unknown"
    with _rate_lock:
        hits = [t for t in _rate_hits.get(key, []) if now - t < 60]
        if len(hits) >= int(os.getenv("RATE_LIMIT_PER_MINUTE", "60")):
            raise HTTPException(429, "Too many requests. Please try again in a minute.")
        hits.append(now)
        _rate_hits[key] = hits

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def require_api_key(x_api_key: str | None = Header(default=None)):
    expected = os.getenv("API_KEY")
    if not expected:
        if os.getenv("ENVIRONMENT", "development") == "production":
            raise HTTPException(503, "API key is not configured.")
        expected = "dev-clinic-key-change-me"
    if not x_api_key or not secrets.compare_digest(x_api_key, expected):
        raise HTTPException(401, "Missing or invalid API key.")

class PatientSignup(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    date_of_birth: date
    email: EmailStr
    password: str = Field(min_length=10, max_length=128)

class PatientSignin(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)

class GoogleAuthPayload(BaseModel):
    credential: str = Field(min_length=20, max_length=8192)
    date_of_birth: date | None = None

class PatientBookingCreate(BaseModel):
    slot_id: str
    reason: str = Field(min_length=5, max_length=1000)

class BookingCreate(BaseModel):
    slot_id: str
    patient_id: str = Field(min_length=2, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    patient_name: str = Field(min_length=2, max_length=120)
    patient_email: EmailStr
    reason: str = Field(default="", max_length=1000)

def slot_json(slot: Slot):
    return {"id": slot.id, "starts_at": slot.starts_at.isoformat(), "duration_minutes": slot.duration_minutes, "available": not slot.is_booked}

def booking_json(booking: Booking, slot: Slot | None = None):
    if slot is None:
        with SessionLocal() as db:
            slot = db.get(Slot, booking.slot_id)
    return {"booking_id": booking.id, "slot_id": booking.slot_id, "patient_id": booking.patient_id,
            "patient_name": booking.patient_name, "patient_email": booking.patient_email, "reason": booking.reason,
            "status": booking.status, "created_at": booking.created_at.isoformat(),
            "appointment": slot.starts_at.isoformat() if slot else None}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/", include_in_schema=False)
def home():
    return FileResponse("frontend/index.html")

@app.get("/signup", include_in_schema=False)
def signup_page():
    return FileResponse("frontend/signup.html")

@app.get("/signin", include_in_schema=False)
def signin_page():
    return FileResponse("frontend/signin.html")

@app.get("/appointments", include_in_schema=False)
def appointments_page():
    return FileResponse("frontend/appointments.html")

@app.get("/bookings", include_in_schema=False)
def bookings_page():
    return FileResponse("frontend/bookings.html")

@app.get("/booking/{booking_id}", include_in_schema=False)
def booking_detail_page(booking_id: str):
    return FileResponse("frontend/booking.html")

@app.get("/api/slots", dependencies=[Depends(require_api_key), Depends(rate_limit)])
def list_slots(day: date | None = Query(default=None), available_only: bool = True, db: Session = Depends(get_db)):
    stmt = select(Slot).where(Slot.starts_at >= datetime.now(timezone.utc))
    if day:
        start = datetime.combine(day, dtime.min, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        stmt = stmt.where(Slot.starts_at >= start, Slot.starts_at < end)
    if available_only:
        stmt = stmt.where(Slot.is_booked.is_(False))
    slots = db.scalars(stmt.order_by(Slot.starts_at).limit(200)).all()
    return {"items": [slot_json(s) for s in slots], "count": len(slots)}

@app.post("/api/bookings", status_code=201, dependencies=[Depends(require_api_key), Depends(rate_limit)])
def create_booking(payload: BookingCreate, idempotency_key: str = Header(min_length=8, max_length=128, alias="Idempotency-Key"), db: Session = Depends(get_db)):
    body_hash = hashlib.sha256(json.dumps({"slot_id": payload.slot_id, "patient_id": payload.patient_id, "patient_name": payload.patient_name, "patient_email": str(payload.patient_email).lower(), "reason": payload.reason}, sort_keys=True).encode()).hexdigest()
    previous = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.patient_id == payload.patient_id, IdempotencyRecord.key == idempotency_key))
    if previous:
        if previous.request_hash != body_hash:
            raise HTTPException(409, "Idempotency key was already used with a different request.")
        booking = db.get(Booking, previous.booking_id)
        if booking:
            return booking_json(booking)
        raise HTTPException(409, "Idempotency record exists but booking is unavailable.")

    # Atomic conditional update is the concurrency guard: only one request can claim a slot.
    claimed = db.execute(update(Slot).where(Slot.id == payload.slot_id, Slot.is_booked.is_(False), Slot.starts_at > datetime.now(timezone.utc)).values(is_booked=True))
    if claimed.rowcount != 1:
        db.rollback()
        # If another identical request just committed, treat this as a successful retry.
        previous = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.patient_id == payload.patient_id, IdempotencyRecord.key == idempotency_key))
        if previous and previous.request_hash == body_hash:
            existing = db.get(Booking, previous.booking_id)
            if existing:
                return booking_json(existing)
        if not db.get(Slot, payload.slot_id):
            raise HTTPException(404, "Appointment slot not found.")
        raise HTTPException(409, "This slot is no longer available.")
    booking = Booking(slot_id=payload.slot_id, patient_id=payload.patient_id, patient_name=payload.patient_name.strip(), patient_email=str(payload.patient_email).lower(), reason=payload.reason.strip())
    db.add(booking)
    db.flush()
    db.add(IdempotencyRecord(patient_id=payload.patient_id, key=idempotency_key, request_hash=body_hash, booking_id=booking.id))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        # A simultaneous retry may have won the unique idempotency-key race.
        previous = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.patient_id == payload.patient_id, IdempotencyRecord.key == idempotency_key))
        if previous and previous.request_hash == body_hash:
            existing = db.get(Booking, previous.booking_id)
            if existing:
                return booking_json(existing)
        raise HTTPException(409, "Could not complete booking; retry with the same idempotency key.")
    db.refresh(booking)
    return booking_json(booking)

@app.get("/api/bookings/{booking_id}", dependencies=[Depends(require_api_key), Depends(rate_limit)])
def get_booking(booking_id: str, db: Session = Depends(get_db)):
    booking = db.get(Booking, booking_id)
    if not booking:
        raise HTTPException(404, "Booking not found.")
    return booking_json(booking)

@app.delete("/api/bookings/{booking_id}", dependencies=[Depends(require_api_key), Depends(rate_limit)])
def cancel_booking(booking_id: str, db: Session = Depends(get_db)):
    booking = db.get(Booking, booking_id)
    if not booking:
        raise HTTPException(404, "Booking not found.")
    if booking.status == "cancelled":
        return {"booking_id": booking.id, "status": "cancelled"}
    slot = db.get(Slot, booking.slot_id)
    booking.status = "cancelled"
    if slot:
        slot.is_booked = False
    db.commit()
    return {"booking_id": booking.id, "status": "cancelled"}

@app.get("/api/patients/{patient_id}/bookings", dependencies=[Depends(require_api_key), Depends(rate_limit)])
def patient_bookings(patient_id: str, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
    stmt = select(Booking).where(Booking.patient_id == patient_id).order_by(Booking.created_at.desc()).offset(offset).limit(limit)
    rows = db.scalars(stmt).all()
    return {"items": [booking_json(row) for row in rows], "limit": limit, "offset": offset, "count": len(rows)}

@app.post("/api/admin/slots/seed", dependencies=[Depends(require_api_key), Depends(rate_limit)])
def seed_slots(days: int = Query(14, ge=1, le=60), db: Session = Depends(get_db)):
    """Create weekday 09:00-16:30 slots for demo/staging; safe to call repeatedly."""
    created = 0
    today = datetime.now(timezone.utc).date()
    for day_offset in range(days):
        target = today + timedelta(days=day_offset)
        if target.weekday() >= 5:
            continue
        for hour in range(9, 17):
            starts = datetime.combine(target, dtime(hour, 0), tzinfo=timezone.utc)
            exists = db.scalar(select(Slot.id).where(Slot.starts_at == starts))
            if not exists and starts > datetime.now(timezone.utc):
                db.add(Slot(starts_at=starts, duration_minutes=30))
                created += 1
    db.commit()
    return {"created": created, "message": "Demo slots are ready."}


# Patient self-service authentication. Passwords are stored with scrypt; signed
# 12-hour bearer tokens are derived from a Render-only secret.
def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")

def _auth_secret() -> bytes:
    secret = os.getenv("AUTH_TOKEN_SECRET")
    if not secret:
        if os.getenv("ENVIRONMENT", "development") == "production":
            raise HTTPException(503, "Patient authentication is not configured. Set AUTH_TOKEN_SECRET in Render.")
        secret = os.getenv("API_KEY", "local-development-auth-secret-change-me")
    return secret.encode()

def _hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return "scrypt$" + _b64(salt) + "$" + _b64(digest)

def _check_password(password: str, stored: str) -> bool:
    try:
        algorithm, salt_text, digest_text = stored.split("$", 2)
        if algorithm != "scrypt":
            return False
        salt = base64.urlsafe_b64decode(salt_text + "=" * (-len(salt_text) % 4))
        expected = base64.urlsafe_b64decode(digest_text + "=" * (-len(digest_text) % 4))
        actual = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False

def _issue_patient_token(patient_id: str) -> str:
    payload = _b64(json.dumps({"sub": patient_id, "exp": int(time.time()) + 43200}, separators=(",", ":")).encode())
    signature = _b64(hmac.new(_auth_secret(), payload.encode(), hashlib.sha256).digest())
    return payload + "." + signature

def current_patient(authorization: str | None = Header(default=None), db: Session = Depends(get_db)) -> PatientAccount:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Sign in to continue.")
    try:
        payload, signature = authorization[7:].split(".", 1)
        expected = _b64(hmac.new(_auth_secret(), payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if int(claims["exp"]) < int(time.time()):
            raise ValueError("expired")
        patient = db.get(PatientAccount, claims["sub"])
        if not patient:
            raise ValueError("patient")
        return patient
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(401, "Your session is invalid or expired. Please sign in again.")

def _patient_booking(db: Session, slot_id: str, patient: PatientAccount, reason: str, key: str):
    body = {"slot_id": slot_id, "patient_id": patient.id, "patient_name": patient.full_name, "patient_email": patient.email, "reason": reason.strip()}
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    prior = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.patient_id == patient.id, IdempotencyRecord.key == key))
    if prior:
        if prior.request_hash != digest:
            raise HTTPException(409, "Idempotency key was already used with a different request.")
        existing = db.get(Booking, prior.booking_id)
        if existing:
            return booking_json(existing)
        raise HTTPException(409, "Previous booking is unavailable.")
    claimed = db.execute(update(Slot).where(Slot.id == slot_id, Slot.is_booked.is_(False), Slot.starts_at > datetime.now(timezone.utc)).values(is_booked=True))
    if claimed.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "This appointment is no longer available.")
    booking = Booking(slot_id=slot_id, patient_id=patient.id, patient_name=patient.full_name, patient_email=patient.email, reason=reason.strip())
    db.add(booking)
    db.flush()
    db.add(IdempotencyRecord(patient_id=patient.id, key=key, request_hash=digest, booking_id=booking.id))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        prior = db.scalar(select(IdempotencyRecord).where(IdempotencyRecord.patient_id == patient.id, IdempotencyRecord.key == key))
        if prior and prior.request_hash == digest:
            existing = db.get(Booking, prior.booking_id)
            if existing:
                return booking_json(existing)
        raise HTTPException(409, "Booking could not be completed. Retry with the same request key.")
    db.refresh(booking)
    return booking_json(booking)

@app.post("/api/auth/signup", status_code=201, dependencies=[Depends(rate_limit)])
def patient_signup(payload: PatientSignup, db: Session = Depends(get_db)):
    email = str(payload.email).lower()
    if payload.date_of_birth >= date.today():
        raise HTTPException(422, "Date of birth must be in the past.")
    if db.scalar(select(PatientAccount.id).where(PatientAccount.email == email)):
        raise HTTPException(409, "An account with this email already exists. Please sign in.")
    patient = PatientAccount(full_name=payload.full_name.strip(), date_of_birth=payload.date_of_birth, email=email, password_hash=_hash_password(payload.password))
    db.add(patient)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "An account with this email already exists. Please sign in.")
    db.refresh(patient)
    return {"access_token": _issue_patient_token(patient.id), "token_type": "bearer", "patient": {"id": patient.id, "full_name": patient.full_name, "date_of_birth": patient.date_of_birth.isoformat(), "email": patient.email}}

@app.post("/api/auth/google", dependencies=[Depends(rate_limit)])
def patient_google_auth(payload: GoogleAuthPayload, db: Session = Depends(get_db)):
    client_id = os.getenv("GOOGLE_CLIENT_ID")
    if not client_id:
        raise HTTPException(503, "Google sign-in is not configured. Set GOOGLE_CLIENT_ID in Render.")
    try:
        claims = google_id_token.verify_oauth2_token(payload.credential, google_requests.Request(), client_id)
    except Exception:
        raise HTTPException(401, "Google credential is invalid or expired. Please try again.")
    if not claims.get("email") or not claims.get("email_verified"):
        raise HTTPException(401, "Google must provide a verified email address.")
    email = str(claims["email"]).lower()
    patient = db.scalar(select(PatientAccount).where(PatientAccount.email == email))
    if not patient:
        if not payload.date_of_birth:
            raise HTTPException(409, "New Google users must complete signup with their date of birth.")
        if payload.date_of_birth >= date.today():
            raise HTTPException(422, "Date of birth must be in the past.")
        patient = PatientAccount(full_name=str(claims.get("name") or email.split("@")[0])[:120],
            date_of_birth=payload.date_of_birth, email=email,
            password_hash=_hash_password(secrets.token_urlsafe(48)))
        db.add(patient)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            patient = db.scalar(select(PatientAccount).where(PatientAccount.email == email))
            if not patient:
                raise HTTPException(409, "Could not create your Google account. Please retry.")
    return {"access_token": _issue_patient_token(patient.id), "token_type": "bearer",
            "patient": {"id": patient.id, "full_name": patient.full_name,
            "date_of_birth": patient.date_of_birth.isoformat(), "email": patient.email}}

@app.post("/api/auth/signin", dependencies=[Depends(rate_limit)])
def patient_signin(payload: PatientSignin, db: Session = Depends(get_db)):
    patient = db.scalar(select(PatientAccount).where(PatientAccount.email == str(payload.email).lower()))
    if not patient or not _check_password(payload.password, patient.password_hash):
        raise HTTPException(401, "Email or password is incorrect.")
    return {"access_token": _issue_patient_token(patient.id), "token_type": "bearer", "patient": {"id": patient.id, "full_name": patient.full_name, "date_of_birth": patient.date_of_birth.isoformat(), "email": patient.email}}

@app.get("/api/auth/me")
def patient_me(patient: PatientAccount = Depends(current_patient)):
    return {"id": patient.id, "full_name": patient.full_name, "date_of_birth": patient.date_of_birth.isoformat(), "email": patient.email}

@app.get("/api/patient/slots")
def patient_available_slots(day: date | None = Query(default=None), db: Session = Depends(get_db)):
    stmt = select(Slot).where(Slot.starts_at >= datetime.now(timezone.utc), Slot.is_booked.is_(False))
    if day:
        start = datetime.combine(day, dtime.min, tzinfo=timezone.utc)
        stmt = stmt.where(Slot.starts_at >= start, Slot.starts_at < start + timedelta(days=1))
    slots = db.scalars(stmt.order_by(Slot.starts_at).limit(200)).all()
    return {"items": [slot_json(slot) for slot in slots], "count": len(slots)}

@app.post("/api/patient/bookings", status_code=201)
def patient_create_booking(payload: PatientBookingCreate, idempotency_key: str = Header(min_length=8, max_length=128, alias="Idempotency-Key"), patient: PatientAccount = Depends(current_patient), db: Session = Depends(get_db)):
    return _patient_booking(db, payload.slot_id, patient, payload.reason, idempotency_key)

@app.get("/api/patient/bookings")
def patient_own_bookings(patient: PatientAccount = Depends(current_patient), db: Session = Depends(get_db)):
    rows = db.scalars(select(Booking).where(Booking.patient_id == patient.id).order_by(Booking.created_at.desc()).limit(100)).all()
    return {"items": [booking_json(row) for row in rows], "count": len(rows)}

@app.get("/api/patient/bookings/{booking_id}")
def patient_own_booking(booking_id: str, patient: PatientAccount = Depends(current_patient), db: Session = Depends(get_db)):
    booking = db.get(Booking, booking_id)
    if not booking or booking.patient_id != patient.id:
        raise HTTPException(404, "Booking not found.")
    return booking_json(booking)
