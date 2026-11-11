import os
os.environ["API_KEY"] = "test-secret-key"
os.environ["DATABASE_URL"] = "sqlite:///./test-clinic.db"

from fastapi.testclient import TestClient
from app.main import app, Base, engine, SessionLocal, Slot
from datetime import datetime, timedelta, timezone

client = TestClient(app)
HEADERS = {"X-API-Key": "test-secret-key"}

def setup_function():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        db.add_all([Slot(starts_at=datetime.now(timezone.utc) + timedelta(days=1, hours=i), duration_minutes=30) for i in range(3)])
        db.commit()

def test_api_key_required():
    assert client.get("/api/slots").status_code == 401
    assert client.get("/api/slots", headers=HEADERS).status_code == 200

def test_booking_lookup_cancel_and_rebook():
    slot = client.get("/api/slots", headers=HEADERS).json()["items"][0]
    payload = {"slot_id": slot["id"], "patient_id": "patient_1", "patient_name": "Ada Patient", "patient_email": "ada@example.com"}
    response = client.post("/api/bookings", headers={**HEADERS, "Idempotency-Key": "booking-key-0001"}, json=payload)
    assert response.status_code == 201
    booking = response.json()
    assert client.get("/api/bookings/" + booking["booking_id"], headers=HEADERS).json()["status"] == "confirmed"
    assert client.post("/api/bookings", headers={**HEADERS, "Idempotency-Key": "booking-key-0001"}, json=payload).json()["booking_id"] == booking["booking_id"]
    assert client.delete("/api/bookings/" + booking["booking_id"], headers=HEADERS).json()["status"] == "cancelled"
    payload["patient_id"] = "patient_2"
    assert client.post("/api/bookings", headers={**HEADERS, "Idempotency-Key": "booking-key-0002"}, json=payload).status_code == 201

def test_conflicting_booking_and_idempotency_mismatch():
    slot = client.get("/api/slots", headers=HEADERS).json()["items"][0]
    payload = {"slot_id": slot["id"], "patient_id": "patient_1", "patient_name": "Ada Patient", "patient_email": "ada@example.com"}
    first = client.post("/api/bookings", headers={**HEADERS, "Idempotency-Key": "booking-key-1001"}, json=payload)
    assert first.status_code == 201
    other = {**payload, "patient_id": "patient_2"}
    assert client.post("/api/bookings", headers={**HEADERS, "Idempotency-Key": "booking-key-1002"}, json=other).status_code == 409
    mismatch = {**payload, "patient_name": "Different Name"}
    assert client.post("/api/bookings", headers={**HEADERS, "Idempotency-Key": "booking-key-1001"}, json=mismatch).status_code == 409

def test_patient_pagination_and_validation():
    slot = client.get("/api/slots", headers=HEADERS).json()["items"][0]
    payload = {"slot_id": slot["id"], "patient_id": "patient_1", "patient_name": "Ada Patient", "patient_email": "not-an-email"}
    assert client.post("/api/bookings", headers={**HEADERS, "Idempotency-Key": "booking-key-2001"}, json=payload).status_code == 422
    assert client.get("/api/patients/patient_1/bookings?limit=1&offset=0", headers=HEADERS).status_code == 200
