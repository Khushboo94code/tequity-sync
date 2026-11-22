"""Service layer: the inbound and outbound sync logic the HTTP endpoints and the demo share.

These functions take an explicit SQLAlchemy session so they are trivially testable against a
throwaway database and reusable from the headless demo without going through HTTP.

- applyInboundWebhook : DESIGN.md section 5 (webhook -> our DB), with dedup, loop-drop, and
  per-field out-of-order handling.
- applyPortalEdit     : DESIGN.md section 6 (portal edit -> local apply + durable outbox row).
- getRecord/getHistory: read helpers for the UI.
"""
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .config import config
from .models import FieldHistory, FieldState, Inbox, Outbox, utcNow
from .rules import isLoopEcho, resolveWrite


def parseOccurredAt(value: str) -> datetime:
    """Parse a Salesforce ISO-8601 timestamp (e.g. '2026-11-02T11:30:04.000Z') to aware UTC."""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _applyFieldWrite(
    session,
    recordId: str,
    objectType: str,
    fieldName: str,
    value,
    ts: datetime,
    actor: str,
    source: str,
    eventId: str | None = None,
) -> bool:
    """Apply one field write under the merge rule. Always appends history; updates current
    state only if the write wins. Returns True if it became the current value.

    This single helper is the one place fieldState/fieldHistory are written, so the inbound
    path, the outbound path, and the backfill all merge identically.
    """
    state = session.get(FieldState, (recordId, fieldName))
    won = resolveWrite(
        ts,
        source,
        state.currentTs if state else None,
        state.currentSource if state else None,
        config.clockSkewSeconds,
        config.sourcePriority,
    )
    session.add(
        FieldHistory(
            recordId=recordId,
            fieldName=fieldName,
            objectType=objectType,
            value=value,
            ts=ts,
            actor=actor,
            source=source,
            eventId=eventId,
            appliedCurrent=won,
        )
    )
    if won:
        if state is None:
            session.add(
                FieldState(
                    recordId=recordId,
                    fieldName=fieldName,
                    objectType=objectType,
                    currentValue=value,
                    currentTs=ts,
                    currentActor=actor,
                    currentSource=source,
                )
            )
        else:
            state.currentValue = value
            state.currentTs = ts
            state.currentActor = actor
            state.currentSource = source
    return won


def applyInboundWebhook(session, payload: dict) -> dict:
    """Apply a Salesforce webhook to our database (DESIGN.md section 5).

    Order of checks, all inside one transaction:
      1. Duplicate: eventId is the inbox primary key -> second delivery is a no-op.
      2. Loop echo: changedBy == integrationUser -> history-only, no state change, no outbox.
      3. Per-field merge: newer occurredAt becomes current; older is kept in history only.

    An inbound change never creates an outbox row (it came from Salesforce already), which is
    the structural backstop to the loop rule.

    Args:
        session: an open SQLAlchemy session (this function commits it).
        payload: the webhook body (eventId, recordId, objectType, occurredAt, changedBy,
            changedFields).

    Returns:
        A result dict describing the outcome, for tests/demo:
        {"status": "duplicate" | "loopDropped" | "applied", ...}.
    """
    eventId = payload["eventId"]
    recordId = payload["recordId"]
    objectType = payload["objectType"]
    changedBy = payload.get("changedBy")
    changedFields = payload.get("changedFields") or {}
    occurredAt = parseOccurredAt(payload["occurredAt"])

    # Fast-path duplicate check (the PK constraint below is the authoritative guard).
    if session.get(Inbox, eventId) is not None:
        return {"status": "duplicate", "eventId": eventId}

    session.add(
        Inbox(
            eventId=eventId,
            recordId=recordId,
            objectType=objectType,
            payload=payload,
            status="processed",
        )
    )
    try:
        session.flush()  # surface a concurrent duplicate now, before applying any state
    except IntegrityError:
        session.rollback()
        return {"status": "duplicate", "eventId": eventId}

    if isLoopEcho(changedBy, config.integrationUser):
        # Our own write echoed back: record it for audit, change nothing, enqueue nothing.
        for fieldName, value in changedFields.items():
            session.add(
                FieldHistory(
                    recordId=recordId,
                    fieldName=fieldName,
                    objectType=objectType,
                    value=value,
                    ts=occurredAt,
                    actor=changedBy,
                    source="salesforce",
                    eventId=eventId,
                    appliedCurrent=False,
                )
            )
        session.commit()
        return {"status": "loopDropped", "eventId": eventId, "fields": list(changedFields)}

    applied, historyOnly = [], []
    for fieldName, value in changedFields.items():
        won = _applyFieldWrite(
            session, recordId, objectType, fieldName, value, occurredAt,
            actor=changedBy or "unknown", source="salesforce", eventId=eventId,
        )
        (applied if won else historyOnly).append(fieldName)
    session.commit()
    return {
        "status": "applied",
        "eventId": eventId,
        "appliedFields": applied,
        "historyOnlyFields": historyOnly,
    }


def recordDict(session, recordId: str) -> dict:
    """Return the current state of a record as {fieldName: {value, ts, actor, source}}."""
    rows = session.execute(
        select(FieldState).where(FieldState.recordId == recordId)
    ).scalars().all()
    return {
        r.fieldName: {
            "value": r.currentValue,
            "ts": r.currentTs.isoformat() if r.currentTs else None,
            "actor": r.currentActor,
            "source": r.currentSource,
        }
        for r in rows
    }


def getRecord(session, recordId: str) -> dict:
    """Read a record's current field values (for the UI / GET endpoint)."""
    return {"recordId": recordId, "fields": recordDict(session, recordId)}


def getHistory(session, recordId: str) -> list[dict]:
    """Read a record's full append-only history, newest first (for the UI / GET endpoint)."""
    rows = session.execute(
        select(FieldHistory)
        .where(FieldHistory.recordId == recordId)
        .order_by(FieldHistory.ts.desc(), FieldHistory.id.desc())
    ).scalars().all()
    return [
        {
            "fieldName": r.fieldName,
            "value": r.value,
            "ts": r.ts.isoformat() if r.ts else None,
            "actor": r.actor,
            "source": r.source,
            "appliedCurrent": r.appliedCurrent,
            "eventId": r.eventId,
        }
        for r in rows
    ]
