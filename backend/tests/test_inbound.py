"""Tests for the inbound webhook path (app/sync.applyInboundWebhook).

Covers three of the four required rules on the inbound side: apply, duplicate, out-of-order,
and loop-drop.
"""
from app.config import config
from app.models import FieldHistory, FieldState
from app.sync import applyInboundWebhook, getRecord


def webhook(eventId, recordId, occurredAt, fields, changedBy="sales.user@client.example",
            objectType="Contact"):
    """Build a webhook payload in the brief's sample shape."""
    return {
        "eventId": eventId,
        "recordId": recordId,
        "objectType": objectType,
        "occurredAt": occurredAt,
        "changedBy": changedBy,
        "changedFields": fields,
    }


def test_applySetsCurrentAndHistory(session):
    res = applyInboundWebhook(session, webhook(
        "evt1", "0031", "2026-11-02T11:30:00.000Z", {"phone": "+91111"}))
    assert res["status"] == "applied"
    assert res["appliedFields"] == ["phone"]
    rec = getRecord(session, "0031")
    assert rec["fields"]["phone"]["value"] == "+91111"
    assert rec["fields"]["phone"]["source"] == "salesforce"
    assert session.query(FieldHistory).count() == 1


def test_duplicateWebhookIsNoOp(session):
    wh = webhook("evtDup", "0031", "2026-11-02T11:30:00.000Z", {"phone": "+91111"})
    first = applyInboundWebhook(session, wh)
    second = applyInboundWebhook(session, wh)
    assert first["status"] == "applied"
    assert second["status"] == "duplicate"
    # exactly one state row and one history row despite two deliveries
    assert session.query(FieldState).count() == 1
    assert session.query(FieldHistory).count() == 1


def test_outOfOrderOlderDoesNotOverwriteButIsKept(session):
    # newer arrives first
    applyInboundWebhook(session, webhook(
        "evtNew", "0031", "2026-11-02T12:00:00.000Z", {"phone": "+91NEW"}))
    # older arrives later
    res = applyInboundWebhook(session, webhook(
        "evtOld", "0031", "2026-11-02T11:00:00.000Z", {"phone": "+91OLD"}))
    assert res["status"] == "applied"
    assert res["historyOnlyFields"] == ["phone"]  # older did not win
    rec = getRecord(session, "0031")
    assert rec["fields"]["phone"]["value"] == "+91NEW"  # current stays newer
    # both writes are on record
    values = {h.value for h in session.query(FieldHistory).all()}
    assert values == {"+91NEW", "+91OLD"}


def test_differentFieldsBothSurvive(session):
    applyInboundWebhook(session, webhook(
        "e1", "0031", "2026-11-02T11:00:00.000Z", {"phone": "+91PHONE"}))
    applyInboundWebhook(session, webhook(
        "e2", "0031", "2026-11-02T11:05:00.000Z", {"city": "Noida"}))
    rec = getRecord(session, "0031")
    assert rec["fields"]["phone"]["value"] == "+91PHONE"
    assert rec["fields"]["city"]["value"] == "Noida"


def test_loopEchoIsDropped(session):
    res = applyInboundWebhook(session, webhook(
        "evtEcho", "0031", "2026-11-02T11:30:00.000Z", {"phone": "+91OURS"},
        changedBy=config.integrationUser))
    assert res["status"] == "loopDropped"
    # no current state created, but an audit row exists marked not-current
    assert session.query(FieldState).count() == 0
    hist = session.query(FieldHistory).all()
    assert len(hist) == 1 and hist[0].appliedCurrent is False
