from __future__ import annotations

import hashlib
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
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, create_engine, select, update
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

class Booking(Base):
    __tablename__ = "bookings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    slot_id: Mapped[str] = mapped_column(ForeignKey("slots.id"), nullable=False, index=True)
    patient_id: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    patient_name: Mapped[str] = mapped_column(String(120), nullable=False)
    patient_email: Mapped[str] = mapped_column(String(254), nullable=False)
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

app = FastAPI(title="Clinic Health Booking API", version="1.0.0", description="Clinic appointment booking API with concurrency safety and idempotency.")
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

class BookingCreate(BaseModel):
    slot_id: str
    patient_id: str = Field(min_length=2, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    patient_name: str = Field(min_length=2, max_length=120)
    patient_email: EmailStr

def slot_json(slot: Slot):
    return {"id": slot.id, "starts_at": slot.starts_at.isoformat(), "duration_minutes": slot.duration_minutes, "available": not slot.is_booked}

def booking_json(booking: Booking, slot: Slot | None = None):
    if slot is None:
        with SessionLocal() as db:
            slot = db.get(Slot, booking.slot_id)
    return {"booking_id": booking.id, "slot_id": booking.slot_id, "patient_id": booking.patient_id,
            "patient_name": booking.patient_name, "patient_email": booking.patient_email,
            "status": booking.status, "created_at": booking.created_at.isoformat(),
            "appointment": slot.starts_at.isoformat() if slot else None}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/", include_in_schema=False)
def home():
    return FileResponse("frontend/index.html")

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
    body_hash = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
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
    booking = Booking(slot_id=payload.slot_id, patient_id=payload.patient_id, patient_name=payload.patient_name.strip(), patient_email=str(payload.patient_email).lower())
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
