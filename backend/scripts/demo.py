"""Headless end-to-end demo of the four required rules, against the fake Salesforce.

Run from a fresh checkout with:
    cd backend && .venv/bin/python scripts/demo.py

Each scenario is self-verifying (asserts the expected outcome) and prints what happened, so a
reviewer can watch the rules hold. The rate cap is set deliberately low so the drain is visibly
paced. No HTTP and no external services — just our service layer and the fake CRM.
"""
import os
import sys
import time
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from app import models  # noqa: E402,F401
from app.backfill import backfillFromSalesforce  # noqa: E402
from app.config import config  # noqa: E402
from app.db import Base  # noqa: E402
from app.fakeSalesforce import FakeSalesforce  # noqa: E402
from app.models import utcNow  # noqa: E402
from app.ratelimit import TokenBucket  # noqa: E402
from app.sync import applyInboundWebhook, applyPortalEdit, getHistory, getRecord  # noqa: E402
from app.worker import drainOnce, pendingCount  # noqa: E402


def freshDb(path):
    """Create a brand-new SQLite database at `path` and return (engine, sessionFactory)."""
    if os.path.exists(path):
        os.remove(path)
    engine = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False}, future=True)
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False, future=True)


def crmWiredTo(Session):
    """A fake CRM whose webhooks are delivered into our inbound path on a fresh session."""
    def deliver(payload):
        s = Session()
        try:
            applyInboundWebhook(s, payload)
        finally:
            s.close()
    return FakeSalesforce(onWebhook=deliver)


def webhook(eventId, recordId, occurredAt, fields, changedBy="sales.user@client.example"):
    return {
        "eventId": eventId, "recordId": recordId, "objectType": "Contact",
        "occurredAt": occurredAt, "changedBy": changedBy, "changedFields": fields,
    }


def banner(title):
    print(f"\n{'=' * 70}\n{title}\n{'=' * 70}")


def rule1_duplicate():
    banner("RULE 1 — a duplicate webhook is applied once")
    engine, Session = freshDb("demo_rule1.db")
    s = Session()
    wh = webhook("evt_1001", "0031", "2026-11-02T11:30:00.000Z", {"phone": "+919876543210"})
    print("deliver eventId=evt_1001 (phone) ->", applyInboundWebhook(s, wh)["status"])
    print("deliver eventId=evt_1001 AGAIN   ->", applyInboundWebhook(s, wh)["status"])
    rec = getRecord(s, "0031")
    hist = getHistory(s, "0031")
    print(f"current phone = {rec['fields']['phone']['value']}; history rows = {len(hist)}")
    assert len(hist) == 1, "duplicate must not create a second history row"
    print("PASS: second delivery was a no-op (one history row).")
    s.close(); engine.dispose()


def rule2_outOfOrder():
    banner("RULE 2 — an older event arriving after a newer one does not overwrite current")
    engine, Session = freshDb("demo_rule2.db")
    s = Session()
    applyInboundWebhook(s, webhook("evt_new", "0031", "2026-11-02T12:00:00.000Z", {"phone": "+91NEW"}))
    print("applied NEWER (12:00) phone=+91NEW")
    res = applyInboundWebhook(s, webhook("evt_old", "0031", "2026-11-02T11:00:00.000Z", {"phone": "+91OLD"}))
    print(f"delivered OLDER (11:00) phone=+91OLD -> historyOnlyFields={res['historyOnlyFields']}")
    rec = getRecord(s, "0031")
    print(f"current phone = {rec['fields']['phone']['value']} (stays NEW); history keeps both")
    assert rec["fields"]["phone"]["value"] == "+91NEW"
    assert {h["value"] for h in getHistory(s, "0031")} == {"+91NEW", "+91OLD"}
    print("PASS: current is the newer value; the older edit is kept on record.")
    s.close(); engine.dispose()


def rule3_loop():
    banner("RULE 3 — our own write does not loop back from Salesforce")
    engine, Session = freshDb("demo_rule3.db")
    crm = crmWiredTo(Session)
    s = Session()
    # Finance edits city in the portal; this enqueues an outbound write.
    applyPortalEdit(s, "0031", "Contact", "finance.user@client.example", {"city": "Noida"})
    print(f"portal edit city=Noida enqueued; pending={pendingCount(s)}")
    # Worker sends it to Salesforce (as the integration user). Salesforce echoes a webhook,
    # which is delivered synchronously into our inbound path during the upsert call.
    counts = drainOnce(s, crm, TokenBucket(1000))
    print(f"worker drained outbox -> {counts}; the echo webhook came back carrying "
          f"changedBy={config.integrationUser}")
    # The echo must be dropped: no new outbox row, city still Noida, one not-current audit row.
    assert pendingCount(s) == 0, "echo must not enqueue another outbound write"
    loopAudit = [h for h in getHistory(s, "0031")
                 if h["source"] == "salesforce" and h["appliedCurrent"] is False]
    print(f"echo recorded as not-current audit rows = {len(loopAudit)}; outbox pending = 0")
    assert len(loopAudit) == 1
    print("PASS: the write crossed to Salesforce once and the echo stopped at our door.")
    s.close(); engine.dispose()


def rule4_restartWhileDown():
    banner("RULE 4 — pending calls survive a restart while Salesforce is down (+ visible cap)")
    path = "demo_rule4.db"
    engine1, Session1 = freshDb(path)
    crm = crmWiredTo(Session1)
    crm.setDown(True)
    s1 = Session1()
    for i in range(6):
        applyPortalEdit(s1, "0031", "Contact", "finance.user@client.example", {"city": f"City{i}"})
    print(f"Salesforce DOWN. Enqueued portal edits; pending={pendingCount(s1)}")
    drainOnce(s1, crm, TokenBucket(1000))
    print(f"attempted drain while down -> pending stays {pendingCount(s1)} (work kept, not lost)")
    s1.close(); engine1.dispose()
    print("... simulating process restart (new engine on the SAME database file) ...")

    engine2 = create_engine(f"sqlite:///{path}", connect_args={"check_same_thread": False}, future=True)
    Session2 = sessionmaker(bind=engine2, expire_on_commit=False, future=True)
    s2 = Session2()
    print(f"after restart, pending still = {pendingCount(s2)} (durable in Postgres/SQLite)")
    assert pendingCount(s2) == 6

    # Salesforce comes back. Drain under a LOW cap (3/s) so the pacing is visible.
    crm.setDown(False)
    cap = 3
    started = time.monotonic()
    counts = drainOnce(s2, crm, TokenBucket(cap), now=utcNow() + timedelta(hours=1))
    elapsed = time.monotonic() - started
    print(f"Salesforce UP. Drained {counts} at cap={cap}/s in {elapsed:.2f}s "
          f"(6 writes at 3/s -> ~{(6 - cap) / cap:.1f}s of pacing)")
    assert counts["done"] == 6 and pendingCount(s2) == 0
    assert elapsed >= (6 - cap) / cap * 0.8, "cap should visibly pace the drain"
    print("PASS: pending calls survived the restart and drained under the enforced cap.")
    s2.close(); engine2.dispose()


def bonus_backfill():
    banner("BONUS — backfill recovers webhooks dropped during the outage")
    engine, Session = freshDb("demo_backfill.db")
    crm = crmWiredTo(Session)
    s = Session()
    crm.salesEdit("0031", "Contact", "phone", "+91DROPPED", changedBy="sales.user@client.example",
                  deliver=False)
    crm.salesEdit("0032", "Contact", "city", "Pune", changedBy="sales.user@client.example",
                  deliver=False)
    print(f"two sales edits made in Salesforce with webhooks DROPPED; we currently hold "
          f"{len(getRecord(s, '0031')['fields'])} fields for 0031")
    result = backfillFromSalesforce(s, crm)
    print(f"backfill via SystemModstamp cursor -> {result}")
    assert getRecord(s, "0031")["fields"]["phone"]["value"] == "+91DROPPED"
    assert getRecord(s, "0032")["fields"]["city"]["value"] == "Pune"
    print("PASS: dropped inbound changes recovered by the cursor pull.")
    s.close(); engine.dispose()


def main():
    print("Tequity sync — four-rule demo against a fake Salesforce (deterministic, offline).")
    rule1_duplicate()
    rule2_outOfOrder()
    rule3_loop()
    rule4_restartWhileDown()
    bonus_backfill()
    banner("ALL SCENARIOS PASSED")
    # Clean up the demo databases.
    for f in ("demo_rule1.db", "demo_rule2.db", "demo_rule3.db", "demo_rule4.db", "demo_backfill.db"):
        if os.path.exists(f):
            os.remove(f)


if __name__ == "__main__":
    main()
