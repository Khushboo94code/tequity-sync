"""Tests for the outbound portal path (app/sync.applyPortalEdit).

Covers local apply + durable outbox enqueue, and the cross-side merge where a newer Salesforce
edit makes a portal write history-only (and therefore not enqueued).
"""
from app.models import Outbox
from app.sync import applyInboundWebhook, applyPortalEdit, getRecord


def webhook(eventId, recordId, occurredAt, fields, changedBy="sales.user@client.example"):
    return {
        "eventId": eventId,
        "recordId": recordId,
        "objectType": "Contact",
        "occurredAt": occurredAt,
        "changedBy": changedBy,
        "changedFields": fields,
    }


def test_portalEditAppliesAndEnqueues(session):
    res = applyPortalEdit(session, "0031", "Contact", "finance.user@client.example",
                          {"city": "Noida"})
    assert res["appliedFields"] == ["city"]
    assert len(res["enqueuedOutboxIds"]) == 1
    rec = getRecord(session, "0031")
    assert rec["fields"]["city"]["value"] == "Noida"
    assert rec["fields"]["city"]["source"] == "portal"
    # exactly one pending outbox row carrying the new value
    rows = session.query(Outbox).all()
    assert len(rows) == 1
    assert rows[0].status == "pending" and rows[0].newValue == "Noida"


def test_portalEditDifferentFieldFromSalesforceBothSurvive(session):
    # Salesforce set phone; finance sets city — the scenario's core requirement.
    applyInboundWebhook(session, webhook(
        "e1", "0031", "2026-11-02T11:00:00.000Z", {"phone": "+91SF"}))
    applyPortalEdit(session, "0031", "Contact", "finance.user@client.example",
                    {"city": "Noida"})
    rec = getRecord(session, "0031")
    assert rec["fields"]["phone"]["value"] == "+91SF"
    assert rec["fields"]["city"]["value"] == "Noida"


def test_portalEditLosesToNewerSalesforceAndIsNotEnqueued(session):
    # A Salesforce edit to the SAME field with a far-future occurredAt already won.
    applyInboundWebhook(session, webhook(
        "e1", "0031", "2099-01-01T00:00:00.000Z", {"city": "Mumbai"}))
    res = applyPortalEdit(session, "0031", "Contact", "finance.user@client.example",
                          {"city": "Noida"})
    # portal write is history-only and NOT sent onward
    assert res["appliedFields"] == []
    assert res["historyOnlyFields"] == ["city"]
    assert res["enqueuedOutboxIds"] == []
    assert session.query(Outbox).count() == 0
    rec = getRecord(session, "0031")
    assert rec["fields"]["city"]["value"] == "Mumbai"  # Salesforce value stays current
