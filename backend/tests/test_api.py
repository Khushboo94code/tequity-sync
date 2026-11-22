"""Smoke tests for the FastAPI HTTP surface (app/main.py).

The handlers are thin pass-throughs to the already-tested service layer, so these verify the wiring:
the app boots (lifespan creates tables + starts the worker), the open webhook endpoint applies and
dedups, the authenticated reads work, and the metrics endpoint responds. The `client`/`authHeaders`
fixtures (conftest.py) inject a throwaway database.
"""


def webhook(eventId, recordId, occurredAt, fields):
    return {
        "eventId": eventId, "recordId": recordId, "objectType": "Contact",
        "occurredAt": occurredAt, "changedBy": "sales.user@client.example",
        "changedFields": fields,
    }


def test_appBootsAndMetricsRespond(client):
    r = client.get("/metrics")        # metrics is open (not a portal-user action)
    assert r.status_code == 200
    assert r.json()["outboxPending"] == 0


def test_webhookAppliesDedupsAndReads(client, authHeaders):
    wh = webhook("evt_1", "0031", "2026-11-02T11:30:00.000Z", {"phone": "+91111"})
    assert client.post("/webhooks/salesforce", json=wh).json()["status"] == "applied"
    assert client.post("/webhooks/salesforce", json=wh).json()["status"] == "duplicate"
    rec = client.get("/records/0031", headers=authHeaders).json()
    assert rec["fields"]["phone"]["value"] == "+91111"
    hist = client.get("/records/0031/history", headers=authHeaders).json()["history"]
    assert len(hist) == 1


def test_cockpitSalesEditAndBackfill(client, authHeaders):
    # a sales edit whose webhook is dropped, then recovered by backfill (cockpit is open)
    client.post("/__control/salesEdit", json={
        "recordId": "0032", "fieldName": "city", "value": "Pune",
        "changedBy": "sales.user@client.example", "deliver": False})
    assert client.get("/records/0032", headers=authHeaders).json()["fields"] == {}
    result = client.post("/__control/backfill").json()
    assert result["appliedFields"] == 1
    assert client.get("/records/0032", headers=authHeaders).json()["fields"]["city"]["value"] == "Pune"
