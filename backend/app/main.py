"""FastAPI app: the HTTP surface for the portal UI, the Salesforce webhook, and demo controls.

Every endpoint exists for a stated reason (the brief asks us to justify each):
  POST /auth/signup, /auth/login        create a portal user / start a session (returns a token).
  GET  /auth/me                         who am I (validates the token).
  GET  /records                         list records for the UI to choose from.          [auth]
  GET  /records/{id} + /history         current field values + append-only audit.         [auth]
  PUT  /portal/records/{id}             a portal save; the ACTOR is the session user,      [auth]
                                        derived server-side (never from the request body).
  POST /webhooks/salesforce             the inbound webhook Salesforce calls.             [open]
  GET  /metrics                         outbox depth + oldest-pending age (watch metrics). [open]
  POST /__control/*                     demo cockpit over the FAKE CRM.                    [open]

Portal reads/edits are behind login so the acting user is a real authenticated identity. The webhook,
metrics, and cockpit are not portal-user actions, so they stay open (a real webhook would use a
signature instead). The fake CRM and the SyncWorker are created once at startup; the worker is the
only CRM caller, so the rate cap stays in one token bucket.
"""
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .auth import createSession, currentUser, hashPassword, verifyPassword
from .backfill import backfillFromSalesforce
from .config import config
from .db import SessionLocal, initDb
from .fakeSalesforce import FakeSalesforce
from .models import FieldState, User
from .sync import applyInboundWebhook, applyPortalEdit, getHistory, getRecord, listRecords
from .worker import SyncWorker, oldestPendingAgeSeconds, pendingCount


def _deliverWebhook(payload: dict) -> None:
    """Deliver a fake-CRM webhook into our inbound path on a fresh session."""
    session = SessionLocal()
    try:
        applyInboundWebhook(session, payload)
    finally:
        session.close()


def seedDemoData() -> None:
    """Seed a few Salesforce-origin demo contacts if the database is empty (SEED_DEMO=1 only).

    Keeps the record list from being blank on first run, without affecting tests (which leave
    SEED_DEMO off).
    """
    session = SessionLocal()
    try:
        if session.query(FieldState).first() is not None:
            return
    finally:
        session.close()
    seeds = [
        ("0031", {"fullName": "Aarti Rao", "phone": "+919000000031",
                  "email": "aarti@client.example", "city": "Delhi", "owner": "sales.user@client.example"}),
        ("0032", {"fullName": "Vikram Shah", "phone": "+919000000032",
                  "email": "vikram@client.example", "city": "Mumbai", "owner": "sales.user@client.example"}),
        ("0033", {"fullName": "Neha Gupta", "phone": "+919000000033",
                  "email": "neha@client.example", "city": "Pune", "owner": "sales.user@client.example"}),
    ]
    for i, (recordId, fields) in enumerate(seeds):
        _deliverWebhook({
            "eventId": f"seed_{recordId}",
            "recordId": recordId,
            "objectType": "Contact",
            "occurredAt": f"2026-11-01T10:0{i}:00.000Z",
            "changedBy": "sales.user@client.example",
            "changedFields": fields,
        })


crm = FakeSalesforce(onWebhook=_deliverWebhook)
worker = SyncWorker(makeSession=SessionLocal, crm=crm)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create tables, optionally seed demo data, and start the drain worker on boot."""
    initDb()
    if config.seedDemo:
        seedDemoData()
    worker.start()
    yield
    worker.stop()


app = FastAPI(title="Tequity two-way sync", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


class Credentials(BaseModel):
    """Signup / login body."""

    email: str
    password: str


class PortalEditBody(BaseModel):
    """Body for a portal save. Note there is no actor field — the actor is the authenticated
    session user, resolved server-side; the client cannot assert who it is."""

    objectType: str = "Contact"
    changedFields: dict


# --- auth ------------------------------------------------------------------------------

@app.post("/auth/signup")
def signup(body: Credentials):
    """Create a portal user and start a session. 409 if the email already exists."""
    session = SessionLocal()
    try:
        if session.get(User, body.email) is not None:
            raise HTTPException(status_code=409, detail="user already exists")
        session.add(User(email=body.email, passwordHash=hashPassword(body.password)))
        session.commit()
        token = createSession(session, body.email)
        return {"token": token, "email": body.email}
    finally:
        session.close()


@app.post("/auth/login")
def login(body: Credentials):
    """Verify credentials and start a session. 401 on bad email/password."""
    session = SessionLocal()
    try:
        user = session.get(User, body.email)
        if user is None or not verifyPassword(body.password, user.passwordHash):
            raise HTTPException(status_code=401, detail="invalid email or password")
        token = createSession(session, body.email)
        return {"token": token, "email": body.email}
    finally:
        session.close()


@app.get("/auth/me")
def me(user: str = Depends(currentUser)):
    """Return the authenticated user's email (validates the token)."""
    return {"email": user}


# --- portal (authenticated) ------------------------------------------------------------

@app.get("/records")
def records(user: str = Depends(currentUser)):
    """List records for the UI to choose from."""
    session = SessionLocal()
    try:
        return {"records": listRecords(session)}
    finally:
        session.close()


@app.get("/records/{recordId}")
def readRecord(recordId: str, user: str = Depends(currentUser)):
    """Return current field values for a record."""
    session = SessionLocal()
    try:
        return getRecord(session, recordId)
    finally:
        session.close()


@app.get("/records/{recordId}/history")
def readHistory(recordId: str, user: str = Depends(currentUser)):
    """Return the append-only history for a record, newest first."""
    session = SessionLocal()
    try:
        return {"recordId": recordId, "history": getHistory(session, recordId)}
    finally:
        session.close()


@app.put("/portal/records/{recordId}")
def portalEdit(recordId: str, body: PortalEditBody, user: str = Depends(currentUser)):
    """Apply a portal edit and enqueue a durable outbound write. The actor is the session user."""
    session = SessionLocal()
    try:
        return applyPortalEdit(session, recordId, body.objectType, user, body.changedFields)
    finally:
        session.close()


# --- open: webhook, metrics --------------------------------------------------------------

@app.post("/webhooks/salesforce")
def inboundWebhook(payload: dict):
    """Receive a Salesforce webhook; dedup, out-of-order, and loop handling live inside."""
    session = SessionLocal()
    try:
        return applyInboundWebhook(session, payload)
    finally:
        session.close()


@app.get("/metrics")
def metrics():
    """Outbox depth and oldest-pending age — the watch metrics (DESIGN.md section 11)."""
    session = SessionLocal()
    try:
        return {
            "outboxPending": pendingCount(session),
            "oldestPendingAgeSeconds": oldestPendingAgeSeconds(session),
            "salesforceDown": crm.down,
        }
    finally:
        session.close()


# --- demo cockpit: controls over the FAKE CRM only --------------------------------------

class SalesEditBody(BaseModel):
    """Cockpit: simulate a sales user editing directly in Salesforce."""

    recordId: str
    objectType: str = "Contact"
    fieldName: str
    value: str
    changedBy: str = "sales.user@client.example"
    deliver: bool = True


@app.post("/__control/down")
def controlDown():
    """Take the fake Salesforce offline (simulate the outage)."""
    crm.setDown(True)
    return {"salesforceDown": True}


@app.post("/__control/up")
def controlUp():
    """Bring the fake Salesforce back online."""
    crm.setDown(False)
    return {"salesforceDown": False}


@app.post("/__control/salesEdit")
def controlSalesEdit(body: SalesEditBody):
    """Simulate a sales user editing in Salesforce; optionally drop the webhook (outage loss)."""
    crm.salesEdit(
        body.recordId, body.objectType, body.fieldName, body.value, body.changedBy,
        deliver=body.deliver,
    )
    return {"ok": True, "delivered": body.deliver and not crm.down}


@app.post("/__control/backfill")
def controlBackfill():
    """Run the SystemModstamp backfill to recover any dropped inbound changes."""
    session = SessionLocal()
    try:
        return backfillFromSalesforce(session, crm)
    finally:
        session.close()
