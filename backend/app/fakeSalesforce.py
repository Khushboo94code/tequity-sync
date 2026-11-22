"""A fake Salesforce CRM we control, standing in for the real one (which is out of scope).

It mirrors the parts of the real contract our design depends on:
  - a record write API (`upsertRecord`) that our worker calls — and that can be toggled DOWN
    to simulate the 6-hour outage;
  - an *echo webhook*: a successful write emits a webhook back to us carrying
    changedBy == the writing user, which exercises the loop rule;
  - a *sales edit* path (`salesEdit`) modelling a human editing in Salesforce, which can
    deliver its webhook or (during an outage) silently drop it;
  - a `SystemModstamp` changed-since query (`queryChangedSince`) for the backfill recovery,
    including `lastModifiedById` so our loop filter also applies during backfill.

`onWebhook` is an opaque callback(payload: dict) the caller wires to our inbound handler.
"""
from datetime import datetime, timedelta

from .errors import TransientError


class FakeSalesforce:
    """In-memory fake CRM. Not thread-safe by design — the demo drives it from one thread."""

    def __init__(self, onWebhook=None):
        self.records: dict[str, dict] = {}
        self.down: bool = False
        self.onWebhook = onWebhook
        self._eventSeq = 0
        # A strictly-increasing stand-in for SystemModstamp so ordering is unambiguous.
        self._clock = datetime(2026, 11, 2, 11, 0, 0)

    # -- internals -------------------------------------------------------------------

    def _nextStamp(self) -> datetime:
        self._clock = self._clock + timedelta(microseconds=1)
        return self._clock

    def _nextEventId(self) -> str:
        self._eventSeq += 1
        return f"evt_{self._eventSeq}"

    def _store(self, recordId, objectType, fieldName, value, lastModifiedById) -> datetime:
        rec = self.records.setdefault(
            recordId, {"objectType": objectType, "fields": {}, "systemModstamp": None,
                       "lastModifiedById": None}
        )
        rec["fields"][fieldName] = value
        stamp = self._nextStamp()
        rec["systemModstamp"] = stamp
        rec["lastModifiedById"] = lastModifiedById
        return stamp

    def _emitWebhook(self, recordId, objectType, fieldName, value, changedBy, occurredAt):
        if self.onWebhook is None:
            return
        self.onWebhook({
            "eventId": self._nextEventId(),
            "recordId": recordId,
            "objectType": objectType,
            "occurredAt": occurredAt.isoformat(),
            "changedBy": changedBy,
            "changedFields": {fieldName: value},
        })

    # -- API our worker calls --------------------------------------------------------

    def upsertRecord(self, recordId, objectType, fieldName, newValue, actor) -> None:
        """Write one field (create or update). Raises TransientError while the CRM is DOWN.

        On success, Salesforce emits a webhook for the change — carrying changedBy == actor
        (our integration user) — which flows back to our inbound path and must be dropped by
        the loop rule.
        """
        if self.down:
            raise TransientError("salesforce unreachable (simulated outage)")
        stamp = self._store(recordId, objectType, fieldName, newValue, lastModifiedById=actor)
        self._emitWebhook(recordId, objectType, fieldName, newValue, actor, stamp)

    # -- modelling a human editing directly in Salesforce ----------------------------

    def salesEdit(self, recordId, objectType, fieldName, value, changedBy, deliver=True) -> None:
        """A sales user edits a record in Salesforce.

        If `deliver` is False, the change is stored but its webhook is NOT sent — modelling a
        webhook Salesforce tried to deliver during our outage that never arrived. Backfill
        recovers these.
        """
        stamp = self._store(recordId, objectType, fieldName, value, lastModifiedById=changedBy)
        if deliver and not self.down:
            self._emitWebhook(recordId, objectType, fieldName, value, changedBy, stamp)

    # -- recovery query --------------------------------------------------------------

    def queryChangedSince(self, sinceTs: datetime) -> list[dict]:
        """Return records whose SystemModstamp is strictly greater than `sinceTs`.

        Stands in for `getUpdated` + a SOQL/Bulk query on SystemModstamp. Includes
        `lastModifiedById` so backfill can skip our own writes (loop consistency).
        """
        out = []
        for recordId, rec in self.records.items():
            if rec["systemModstamp"] is not None and rec["systemModstamp"] > sinceTs:
                out.append({
                    "recordId": recordId,
                    "objectType": rec["objectType"],
                    "fields": dict(rec["fields"]),
                    "systemModstamp": rec["systemModstamp"],
                    "lastModifiedById": rec["lastModifiedById"],
                })
        return sorted(out, key=lambda r: r["systemModstamp"])

    # -- outage control --------------------------------------------------------------

    def setDown(self, down: bool) -> None:
        """Toggle the simulated outage."""
        self.down = down
