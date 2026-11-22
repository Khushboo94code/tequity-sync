"""Outbound sync worker — the only place we call Salesforce.

Because every Salesforce request goes through here, the rate cap is enforced in exactly one
token bucket (including retries). The worker drains the durable `outbox`: claim due pending
rows, send them under the cap, and on failure either back off (transient) or fail+alert
(permanent). Nothing is held in memory, so a restart simply resumes from the table.

The worker depends on a `crm` object exposing:
    upsertRecord(recordId, objectType, fieldName, newValue, actor) -> None
raising TransientError / PermanentError (see app/errors.py). The real Salesforce client and
the fake CRM both implement this.
"""
import threading
from datetime import timedelta

from sqlalchemy import func, select

from .config import config
from .errors import PermanentError, TransientError
from .models import Outbox, utcNow
from .ratelimit import TokenBucket

BACKOFF_BASE_SECONDS = 2
MAX_ATTEMPTS = 8


def requeueStaleInflight(session) -> int:
    """Return rows left 'inflight' by a crashed worker to 'pending' on restart.

    A production system uses a lease timestamp and only requeues inflight rows older than the
    lease; for the single-worker demo, requeuing all inflight on startup is correct and simple.
    """
    rows = session.execute(
        select(Outbox).where(Outbox.status == "inflight")
    ).scalars().all()
    for r in rows:
        r.status = "pending"
    session.commit()
    return len(rows)


def claimDuePending(session, limit: int = 100, now=None):
    """Claim up to `limit` due pending rows, marking them 'inflight'.

    On Postgres we use FOR UPDATE SKIP LOCKED so multiple workers never claim the same row;
    on SQLite (single-worker demo) a plain ordered select suffices.
    """
    now = now or utcNow()
    stmt = (
        select(Outbox)
        .where(Outbox.status == "pending", Outbox.nextAttemptAt <= now)
        .order_by(Outbox.id)
        .limit(limit)
    )
    if session.bind.dialect.name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    rows = session.execute(stmt).scalars().all()
    for r in rows:
        r.status = "inflight"
    session.commit()
    return rows


def _backoffSeconds(attempts: int) -> int:
    """Exponential backoff: 2, 4, 8, ... seconds."""
    return BACKOFF_BASE_SECONDS * (2 ** max(0, attempts - 1))


def processRow(session, crm, bucket: TokenBucket, row: Outbox) -> str:
    """Send one outbox row to the CRM under the rate cap, applying retry/backoff.

    Returns the resulting status: 'done', 'pending' (will retry), or 'failed'.
    """
    bucket.acquire()  # the cap applies to this call, including retries
    try:
        # We authenticate to Salesforce as the integration user, so the write (and the webhook
        # Salesforce echoes for it) is attributed to that identity — which is what the inbound
        # loop rule and the backfill own-write skip key on. row.actor (the portal editor) is
        # already recorded in our history; Salesforce only ever sees the integration user.
        crm.upsertRecord(
            row.recordId, row.objectType, row.fieldName, row.newValue, config.integrationUser
        )
    except TransientError as exc:
        row.attempts += 1
        if row.attempts >= MAX_ATTEMPTS:
            row.status = "failed"
            row.lastError = f"transient, gave up after {row.attempts}: {exc}"
        else:
            row.status = "pending"
            row.nextAttemptAt = utcNow() + timedelta(seconds=_backoffSeconds(row.attempts))
            row.lastError = str(exc)
        session.commit()
        return row.status
    except PermanentError as exc:
        row.attempts += 1
        row.status = "failed"
        row.lastError = f"permanent: {exc}"
        session.commit()
        return "failed"
    row.status = "done"
    row.lastError = None
    session.commit()
    return "done"


def drainOnce(session, crm, bucket: TokenBucket, limit: int = 100, now=None) -> dict:
    """Claim and process one batch of due rows. Returns a {status: count} summary."""
    rows = claimDuePending(session, limit=limit, now=now)
    counts = {"done": 0, "pending": 0, "failed": 0}
    for row in rows:
        status = processRow(session, crm, bucket, row)
        counts[status] = counts.get(status, 0) + 1
    return counts


def pendingCount(session) -> int:
    """Number of rows still waiting to be sent (for metrics / the demo)."""
    return session.execute(
        select(func.count()).select_from(Outbox).where(Outbox.status == "pending")
    ).scalar_one()


def oldestPendingAgeSeconds(session) -> float | None:
    """Age in seconds of the oldest pending row, or None if the queue is empty (metric 1)."""
    oldest = session.execute(
        select(func.min(Outbox.createdAt)).where(Outbox.status == "pending")
    ).scalar_one()
    if oldest is None:
        return None
    return (utcNow() - oldest).total_seconds()


class SyncWorker:
    """Background thread that polls the outbox and drains it under the cap.

    Used by the FastAPI app. On start it requeues any stale inflight rows (restart recovery),
    then loops drainOnce() on a short poll interval.
    """

    def __init__(self, makeSession, crm, ratePerSecond: float | None = None,
                 pollInterval: float = 0.2):
        self.makeSession = makeSession
        self.crm = crm
        self.bucket = TokenBucket(ratePerSecond or config.salesforceRateCap)
        self.pollInterval = pollInterval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        session = self.makeSession()
        try:
            requeueStaleInflight(session)
        finally:
            session.close()
        while not self._stop.is_set():
            session = self.makeSession()
            try:
                drainOnce(session, self.crm, self.bucket)
            finally:
                session.close()
            self._stop.wait(self.pollInterval)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
