"""Tests for the fake Salesforce CRM and the post-outage backfill.

Covers the real echo-webhook loop path, normal webhook delivery, backfill recovery of dropped
webhooks, and backfill skipping our own integration-user writes.
"""
from app.backfill import backfillFromSalesforce
from app.config import config
from app.fakeSalesforce import FakeSalesforce
from app.models import FieldHistory, FieldState
from app.sync import applyInboundWebhook, getRecord


def makeCrm(makeSession):
    """A fake CRM whose webhooks are delivered into our inbound path on a fresh session."""
    def deliver(payload):
        s = makeSession()
        try:
            applyInboundWebhook(s, payload)
        finally:
            s.close()
    return FakeSalesforce(onWebhook=deliver)


def test_echoWebhookIsDroppedEndToEnd(session, makeSession):
    # Our worker writes to Salesforce as the integration user; the echo must be dropped.
    crm = makeCrm(makeSession)
    crm.upsertRecord("0031", "Contact", "city", "Noida", actor=config.integrationUser)
    # No current state should exist (echo dropped), only a not-current audit row.
    assert session.query(FieldState).count() == 0
    hist = session.query(FieldHistory).all()
    assert len(hist) == 1 and hist[0].appliedCurrent is False


def test_deliveredSalesEditAppliesViaWebhook(session, makeSession):
    crm = makeCrm(makeSession)
    crm.salesEdit("0031", "Contact", "phone", "+91SALES", changedBy="sales@x", deliver=True)
    rec = getRecord(session, "0031")
    assert rec["fields"]["phone"]["value"] == "+91SALES"


def test_backfillRecoversDroppedWebhooks(session, makeSession):
    crm = makeCrm(makeSession)
    # Webhooks dropped during the outage: stored in SF, never delivered to us.
    crm.salesEdit("0031", "Contact", "phone", "+91DROP", changedBy="sales@x", deliver=False)
    crm.salesEdit("0032", "Contact", "city", "Pune", changedBy="sales@x", deliver=False)
    assert getRecord(session, "0031")["fields"] == {}  # we missed them

    result = backfillFromSalesforce(session, crm)
    assert result["appliedFields"] == 2
    assert getRecord(session, "0031")["fields"]["phone"]["value"] == "+91DROP"
    assert getRecord(session, "0032")["fields"]["city"]["value"] == "Pune"


def test_backfillSkipsOwnWrites(session, makeSession):
    # One record last-modified by us (no echo wired), one by a sales user.
    crm = FakeSalesforce(onWebhook=None)
    crm.upsertRecord("0031", "Contact", "city", "OURS", actor=config.integrationUser)
    crm.salesEdit("0032", "Contact", "city", "THEIRS", changedBy="sales@x", deliver=False)

    result = backfillFromSalesforce(session, crm)
    assert result["appliedFields"] == 1  # only the sales edit
    assert result["skippedOwnWrites"] == 1
    assert getRecord(session, "0032")["fields"]["city"]["value"] == "THEIRS"
    assert "0031" not in getRecord(session, "0031")["fields"] or \
        getRecord(session, "0031")["fields"] == {}


def test_backfillAdvancesCursorAndIsIdempotent(session, makeSession):
    crm = makeCrm(makeSession)
    crm.salesEdit("0031", "Contact", "phone", "+91ONE", changedBy="sales@x", deliver=False)
    first = backfillFromSalesforce(session, crm)
    # Running again with no new changes applies nothing (cursor advanced).
    second = backfillFromSalesforce(session, crm)
    assert first["appliedFields"] == 1
    assert second["appliedFields"] == 0
    assert second["cursor"] == first["cursor"]
