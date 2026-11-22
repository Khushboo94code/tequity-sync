"""The correctness core: pure decision functions with no I/O.

Everything that decides *which edit is current*, *whether a webhook is our own echo*, and
*which fields actually changed* lives here as side-effect-free functions. Keeping them pure
makes the four required rules exhaustively unit-testable without a database.

See DESIGN.md sections 7-9.
"""
from datetime import datetime, timezone


def asUtc(dt: datetime | None) -> datetime | None:
    """Coerce a datetime to timezone-aware UTC.

    SQLite returns tz-naive datetimes; Postgres returns tz-aware. Normalising here lets the
    merge rule compare timestamps safely regardless of the backing database. Naive values
    are assumed to already be UTC (which is how we always write them).
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _priorityRank(source: str, sourcePriority: tuple) -> int:
    """Index of a source in the tie-break priority (lower = wins). Unknown sources rank last."""
    try:
        return sourcePriority.index(source)
    except ValueError:
        return len(sourcePriority)


def resolveWrite(
    incomingTs: datetime,
    incomingSource: str,
    currentTs: datetime | None,
    currentSource: str | None,
    clockSkewSeconds: float,
    sourcePriority: tuple,
) -> bool:
    """Decide whether an incoming write should become the current value of a field.

    This is per-field last-writer-wins by timestamp, with a deterministic tie-break so two
    nodes (and the two different clocks behind Salesforce's occurredAt and the portal's
    commit time) always reach the same answer.

    Args:
        incomingTs: timestamp of the incoming write (occurredAt, or portal commit time).
        incomingSource: 'salesforce' or 'portal'.
        currentTs: timestamp of the stored current value, or None if the field is unset.
        currentSource: source of the stored current value, or None.
        clockSkewSeconds: writes within this window of each other are treated as a tie.
        sourcePriority: tie-break order; the earlier source wins (Salesforce by default).

    Returns:
        True if the incoming write wins and should replace the current value; False if the
        stored value stays current (the incoming write is still kept in history by the caller).
    """
    if currentTs is None:
        return True

    incoming = asUtc(incomingTs)
    current = asUtc(currentTs)
    delta = (incoming - current).total_seconds()

    if abs(delta) <= clockSkewSeconds:
        # Tie window: the two timestamps are too close (or from skewed clocks) to trust.
        if incomingSource == currentSource:
            # Same origin: the later edit wins (ties resolve to the incoming write).
            return incoming >= current
        # Different origins: a fixed priority decides, deterministically.
        return _priorityRank(incomingSource, sourcePriority) < _priorityRank(
            currentSource, sourcePriority
        )

    # Outside the skew window: strictly newer wins.
    return delta > 0


def isLoopEcho(changedBy: str | None, integrationUser: str) -> bool:
    """True if a webhook was caused by our own outbound write.

    Our sync worker authenticates to Salesforce as the integration user, so Salesforce's
    echo webhook for that write carries changedBy == integrationUser. Detecting it is the
    primary loop-stopping rule (DESIGN.md section 9).
    """
    return changedBy is not None and changedBy == integrationUser


def diffFullRecord(fullFields: dict, currentValuesByField: dict) -> dict:
    """Reduce a full-record payload to only the fields whose value actually changed.

    We assume webhooks carry changed fields only (assumption 2). This is the rule for the
    *other* case: if a full record ever arrives, we compare each field against what we hold
    and act on the differences only — never blanket-overwriting unlisted fields.

    Args:
        fullFields: {fieldName: value} for the whole record as received.
        currentValuesByField: {fieldName: currentValue} we currently store.

    Returns:
        {fieldName: value} containing only the entries that differ from stored state.
    """
    changed = {}
    for fieldName, value in fullFields.items():
        if currentValuesByField.get(fieldName) != value:
            changed[fieldName] = value
    return changed
