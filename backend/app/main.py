"""FastAPI app: the HTTP surface for the portal UI, the Salesforce webhook, and demo controls.

Every endpoint exists for a stated reason (the brief asks us to justify each):
  POST /webhooks/salesforce        the inbound webhook Salesforce calls -> applyInboundWebhook.
  PUT  /portal/records/{recordId}  a portal save -> applyPortalEdit (enqueues a durable write).
  GET  /records/{recordId}         current field values, for the UI to render state.
  GET  /records/{recordId}/history the append-only audit, for the UI's history timeline.
  GET  /metrics                    the watch metrics from DESIGN.md section 11.
  POST /__control/*                demo-cockpit controls over the FAKE CRM only (never real SF);
                                   they let a reviewer trigger the four rules live from the UI.

The fake CRM and the background SyncWorker are created once at startup. The worker is the only
caller of the CRM, so the rate cap stays enforced in a single token bucket.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .backfill import backfillFromSalesforce
from .db import SessionLocal, initDb
from .fakeSalesforce import FakeSalesforce
from .sync import applyInboundWebhook, applyPortalEdit, getHistory, getRecord
from .worker import SyncWorker, oldestPendingAgeSeconds, pendingCount


def _deliverWebhook(payload: dict) -> None:
    """Deliver a fake-CRM webhook into our inbound path on a fresh session."""
    session = SessionLocal()
    try:
        applyInboundWebhook(session, payload)
    finally:
        session.close()


crm = FakeSalesforce(onWebhook=_deliverWebhook)
worker = SyncWorker(makeSession=SessionLocal, crm=crm)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create tables and start the drain worker on boot; stop the worker on shutdown."""
    initDb()
    worker.start()
    yield
    worker.stop()


app = FastAPI(title="Tequity two-way sync", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


class PortalEditBody(BaseModel):
    """Body for a portal save. The time/actor come from the server/session, not the client."""

    objectType: str = "Contact"
    editedBy: str
    changedFields: dict


class SalesEditBody(BaseModel):
    """Cockpit: simulate a sales user editing directly in Salesforce."""

    recordId: str
    objectType: str = "Contact"
    fieldName: str
    value: str
    changedBy: str = "sales.user@client.example"
    deliver: bool = True


@app.post("/webhooks/salesforce")
def inboundWebhook(payload: dict):
    """Receive a Salesforce webhook; dedup, out-of-order, and loop handling live inside."""
    session = SessionLocal()
    try:
        return applyInboundWebhook(session, payload)
    finally:
        session.close()


@app.put("/portal/records/{recordId}")
def portalEdit(recordId: str, body: PortalEditBody):
    """Apply a portal edit locally and enqueue a durable outbound write to Salesforce."""
    session = SessionLocal()
    try:
        return applyPortalEdit(session, recordId, body.objectType, body.editedBy, body.changedFields)
    finally:
        session.close()


@app.get("/records/{recordId}")
def readRecord(recordId: str):
    """Return current field values for a record."""
    session = SessionLocal()
    try:
        return getRecord(session, recordId)
    finally:
        session.close()


@app.get("/records/{recordId}/history")
def readHistory(recordId: str):
    """Return the append-only history for a record, newest first."""
    session = SessionLocal()
    try:
        return {"recordId": recordId, "history": getHistory(session, recordId)}
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
