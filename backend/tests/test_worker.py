"""Tests for the rate limiter and the outbound worker.

Covers cap enforcement, transient-keeps-work-and-backs-off, permanent-fails, and the
restart-durability property (pending rows survive a simulated process restart).
"""
from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models  # noqa: F401
from app.db import Base
from app.errors import PermanentError, TransientError
from app.models import Outbox, utcNow
from app.ratelimit import TokenBucket
from app.sync import applyPortalEdit
from app.worker import drainOnce, pendingCount, requeueStaleInflight


class FakeClock:
    """Controllable clock: sleep() advances virtual time instead of blocking."""

    def __init__(self):
        self.t = 0.0

    def time(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class StubCrm:
    """A minimal CRM implementing upsertRecord, with switchable failure modes."""

    def __init__(self):
        self.calls = []
        self.mode = "ok"  # 'ok' | 'down' | 'reject'

    def upsertRecord(self, recordId, objectType, fieldName, newValue, actor):
        if self.mode == "down":
            raise TransientError("salesforce unreachable")
        if self.mode == "reject":
            raise PermanentError("400 invalid field")
        self.calls.append((recordId, fieldName, newValue, actor))


def test_tokenBucketEnforcesRate():
    clock = FakeClock()
    bucket = TokenBucket(5, timeFn=clock.time, sleepFn=clock.sleep)
    for _ in range(10):
        bucket.acquire()
    # 5 free (initial capacity) + 5 paced at 5/s => ~1.0s of virtual time
    assert clock.t == 5 / 5  # (10 - capacity 5) / rate 5 = 1.0


def test_drainSendsPendingRows(session):
    applyPortalEdit(session, "0031", "Contact", "finance@x", {"city": "Noida"})
    applyPortalEdit(session, "0031", "Contact", "finance@x", {"email": "a@b.com"})
    crm = StubCrm()
    counts = drainOnce(session, crm, TokenBucket(1000))
    assert counts["done"] == 2
    assert pendingCount(session) == 0
    assert len(crm.calls) == 2


def test_transientKeepsWorkAndBacksOff(session):
    applyPortalEdit(session, "0031", "Contact", "finance@x", {"city": "Noida"})
    crm = StubCrm()
    crm.mode = "down"
    counts = drainOnce(session, crm, TokenBucket(1000))
    assert counts["pending"] == 1          # kept, not lost
    assert pendingCount(session) == 1
    assert crm.calls == []
    row = session.query(Outbox).one()
    assert row.attempts == 1
    assert row.nextAttemptAt > utcNow()    # backed off into the future


def test_permanentMarksFailed(session):
    applyPortalEdit(session, "0031", "Contact", "finance@x", {"city": "Noida"})
    crm = StubCrm()
    crm.mode = "reject"
    counts = drainOnce(session, crm, TokenBucket(1000))
    assert counts["failed"] == 1
    row = session.query(Outbox).one()
    assert row.status == "failed" and "permanent" in row.lastError


def test_restartDurabilityWhileSalesforceDown(dbUrl):
    """Pending calls must still be there after our process restarts while Salesforce is down."""
    engine1 = create_engine(dbUrl, connect_args={"check_same_thread": False}, future=True)
    Base.metadata.create_all(engine1)
    Session1 = sessionmaker(bind=engine1, expire_on_commit=False, future=True)

    # 1. Portal edits enqueue while Salesforce is DOWN.
    s1 = Session1()
    applyPortalEdit(s1, "0031", "Contact", "finance@x", {"city": "Noida"})
    applyPortalEdit(s1, "0031", "Contact", "finance@x", {"phone": "+91999"})
    crm = StubCrm()
    crm.mode = "down"
    drainOnce(s1, crm, TokenBucket(1000))
    assert pendingCount(s1) == 2  # both kept
    s1.close()
    engine1.dispose()  # simulate the process going away

    # 2. "Restart": a brand-new engine/session on the SAME database file.
    engine2 = create_engine(dbUrl, connect_args={"check_same_thread": False}, future=True)
    Session2 = sessionmaker(bind=engine2, expire_on_commit=False, future=True)
    s2 = Session2()
    requeueStaleInflight(s2)
    assert pendingCount(s2) == 2  # pending calls survived the restart

    # 3. Salesforce comes back; drain with a future 'now' to clear any backoff.
    crm.mode = "ok"
    counts = drainOnce(s2, crm, TokenBucket(1000), now=utcNow() + timedelta(hours=1))
    assert counts["done"] == 2
    assert pendingCount(s2) == 0
    assert len(crm.calls) == 2
    s2.close()
