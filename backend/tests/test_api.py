"""Smoke tests for the FastAPI HTTP surface (app/main.py).

The handlers are thin pass-throughs to the already-tested service layer, so these verify the
wiring: the app boots (lifespan creates tables + starts the worker), the webhook endpoint
applies and dedups, reads work, and the metrics endpoint responds. An isolated SQLite DB is
injected so this never touches the dev database.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from fastapi.testclient import TestClient

import app.db as db
import app.main as main


def makeClient(tmp_path, monkeypatch):
    """A TestClient wired to a throwaway database."""
    engine = create_engine(
        f"sqlite:///{tmp_path}/api.db", connect_args={"check_same_thread": False}, future=True
    )
    Sess = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", Sess)
    monkeypatch.setattr(main, "SessionLocal", Sess)
    monkeypatch.setattr(main.worker, "makeSession", Sess)
    return TestClient(main.app)


def webhook(eventId, recordId, occurredAt, fields):
    return {
        "eventId": eventId, "recordId": recordId, "objectType": "Contact",
        "occurredAt": occurredAt, "changedBy": "sales.user@client.example",
        "changedFields": fields,
    }


def test_appBootsAndMetricsRespond(tmp_path, monkeypatch):
    with makeClient(tmp_path, monkeypatch) as client:
        r = client.get("/metrics")
        assert r.status_code == 200
        assert r.json()["outboxPending"] == 0


def test_webhookAppliesDedupsAndReads(tmp_path, monkeypatch):
    with makeClient(tmp_path, monkeypatch) as client:
        wh = webhook("evt_1", "0031", "2026-11-02T11:30:00.000Z", {"phone": "+91111"})
        assert client.post("/webhooks/salesforce", json=wh).json()["status"] == "applied"
        assert client.post("/webhooks/salesforce", json=wh).json()["status"] == "duplicate"
        rec = client.get("/records/0031").json()
        assert rec["fields"]["phone"]["value"] == "+91111"
        hist = client.get("/records/0031/history").json()["history"]
        assert len(hist) == 1


def test_cockpitSalesEditAndBackfill(tmp_path, monkeypatch):
    with makeClient(tmp_path, monkeypatch) as client:
        # a sales edit whose webhook is dropped, then recovered by backfill
        client.post("/__control/salesEdit", json={
            "recordId": "0032", "fieldName": "city", "value": "Pune",
            "changedBy": "sales.user@client.example", "deliver": False})
        assert client.get("/records/0032").json()["fields"] == {}
        result = client.post("/__control/backfill").json()
        assert result["appliedFields"] == 1
        assert client.get("/records/0032").json()["fields"]["city"]["value"] == "Pune"
