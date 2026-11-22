"""Post-outage inbound recovery: pull everything Salesforce changed since our cursor.

Webhooks Salesforce tried to send during an outage may never arrive. On reconnect we recover
them with a `SystemModstamp` cursor pull (getUpdated + SOQL/Bulk in production; queryChangedSince
on the fake CRM here), apply each change through the same per-field merge as the inbound path —
so out-of-order still resolves correctly — and advance the cursor. Records last modified by our
integration user are skipped, so the loop rule holds through backfill too (DESIGN.md section 10).
"""
from datetime import datetime

from .config import config
from .models import SyncCursor, utcNow
from .sync import _applyFieldWrite

CURSOR_KEY = "*"  # a single global cursor across object types keeps the demo simple
EPOCH = datetime(1970, 1, 1)


def getCursor(session) -> datetime:
    """The high-water SystemModstamp we have already applied (EPOCH if none yet)."""
    row = session.get(SyncCursor, CURSOR_KEY)
    return row.lastSystemModstamp if row else EPOCH


def setCursor(session, stamp: datetime) -> None:
    """Persist the new high-water mark."""
    row = session.get(SyncCursor, CURSOR_KEY)
    if row:
        row.lastSystemModstamp = stamp
        row.updatedAt = utcNow()
    else:
        session.add(SyncCursor(objectType=CURSOR_KEY, lastSystemModstamp=stamp, updatedAt=utcNow()))


def backfillFromSalesforce(session, crm) -> dict:
    """Recover inbound changes missed during an outage. Returns a summary dict.

    Args:
        session: an open session (this function commits it).
        crm: the Salesforce client exposing queryChangedSince(sinceTs) -> list of
            {recordId, objectType, fields, systemModstamp, lastModifiedById}.
    """
    cursor = getCursor(session)
    changes = crm.queryChangedSince(cursor)
    applied, skippedOwn = 0, 0
    maxStamp = cursor
    for change in changes:  # queryChangedSince returns them ordered by systemModstamp
        stamp = change["systemModstamp"]
        maxStamp = max(maxStamp, stamp)
        if change["lastModifiedById"] == config.integrationUser:
            skippedOwn += 1  # our own write echoing through the pull — ignore it
            continue
        for fieldName, value in change["fields"].items():
            _applyFieldWrite(
                session,
                change["recordId"],
                change["objectType"],
                fieldName,
                value,
                stamp,
                actor=change["lastModifiedById"] or "unknown",
                source="salesforce",
                eventId=None,
            )
            applied += 1
    setCursor(session, maxStamp)
    session.commit()
    return {"appliedFields": applied, "skippedOwnWrites": skippedOwn, "cursor": maxStamp}
