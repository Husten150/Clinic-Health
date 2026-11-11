# Clinic Health

A small clinic appointment booking API and responsive browser UI, built with FastAPI and SQLAlchemy.

## Features

- Patient signup and sign-in with date of birth, unique email and scrypt password hashing.
- Patient-only bearer sessions with 12-hour expiry and a dedicated signing secret.
- Patient-facing slot browsing, appointment booking with a reason for visit, and private booking history.
- List available appointment slots, optionally filtered by UTC date.
- Book a slot and retrieve a booking confirmation.
- API-key authentication using `X-API-Key`; constant-time key comparison.
- Pydantic validation for patient identifiers, names and email addresses.
- Atomic conditional slot claiming in the database to prevent concurrent double-booking.
- Cancel bookings and release the slot.
- Idempotent booking requests using `Idempotency-Key`; replays return the original booking and mismatched payload reuse returns HTTP 409.
- Paginated patient booking history.
- Per-IP request throttling (60 requests/minute by default).
- Health endpoint, OpenAPI docs and Render deployment blueprint.

## Run locally

Python 3.11+ is recommended.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000. The development API key defaults to `dev-clinic-key-change-me`. In the page, enter that key and a patient reference. Generate demo slots once:

```powershell
$headers = @{ "X-API-Key" = "dev-clinic-key-change-me" }
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/api/admin/slots/seed?days=14" -Headers $headers
```

API documentation: http://127.0.0.1:8000/docs

## Patient account API

- `POST /api/auth/signup` — create an account with full name, date of birth, email and password (minimum 10 characters).
- `POST /api/auth/signin` — sign in and receive a 12-hour bearer token.
- `GET /api/auth/me` — get the signed-in patient profile.
- `GET /api/patient/slots?day=YYYY-MM-DD` — list available slots.
- `POST /api/patient/bookings` — book a slot with bearer authentication and an `Idempotency-Key`; body contains `slot_id` and `reason`.
- `GET /api/patient/bookings` — list only the signed-in patient's bookings.
- `GET /api/patient/bookings/{booking_id}` — retrieve only the signed-in patient's own booking.

The patient-facing page does not need the administrator API key. Set `AUTH_TOKEN_SECRET` as a separate secret in Render; the Blueprint requests a generated value. Never commit either secret to GitHub.

## API

All `/api` routes require `X-API-Key`. The seed endpoint is intended for local/demo setup and should be disabled or protected by a separate administrator credential in a real clinic deployment.

| Method | Endpoint | Purpose |
|---|---|---|
| GET | `/api/slots?day=YYYY-MM-DD` | List future available slots |
| POST | `/api/bookings` | Book a slot; requires `Idempotency-Key` |
| GET | `/api/bookings/{booking_id}` | Look up confirmation |
| DELETE | `/api/bookings/{booking_id}` | Cancel and release slot |
| GET | `/api/patients/{patient_id}/bookings?limit=20&offset=0` | Paginated patient history |
| POST | `/api/admin/slots/seed?days=14` | Seed weekday demo slots |

Booking body:

```json
{
  "slot_id": "slot-uuid",
  "patient_id": "patient_1024",
  "patient_name": "Ada Patient",
  "patient_email": "ada@example.com"
}
```

A successful booking returns HTTP 201 and a `booking_id`. Use the same idempotency key and exact body when retrying a request after a network timeout.

## Tests

```powershell
pytest -q
```

Tests use a local SQLite database and cover authentication, validation, booking, lookup, cancellation/rebooking, conflicts, idempotency and pagination.

## Deploy to Render

1. Push this repository to GitHub.
2. In Render, choose **New → Blueprint** and select this repository.
3. Render reads `render.yaml`, provisions PostgreSQL and generates `API_KEY`.
4. After deploy, open the service URL and use the generated API key in the web page.
5. Seed demo appointment times by calling `POST /api/admin/slots/seed?days=14` with the generated `X-API-Key` header.

The included limiter is process-local. For multi-instance production deployments, use a shared Redis limiter. The demo seed route should be restricted/removed before handling real patient traffic. This sample is not a certified clinical system: add staff authorization, audit logging, privacy controls, backups, monitoring, and applicable healthcare compliance safeguards before production use. All appointment timestamps are UTC in this demo.
