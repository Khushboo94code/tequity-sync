"""Unit tests for the pure merge/loop/diff rules (app/rules.py).

These cover the decision core that the four required behaviours rest on, with no database.
"""
from datetime import datetime, timedelta, timezone

from app.rules import asUtc, diffFullRecord, isLoopEcho, resolveWrite

PRIORITY = ("salesforce", "portal")
SKEW = 2.0


def ts(seconds: int) -> datetime:
    """A deterministic UTC timestamp offset by `seconds` from a fixed base."""
    return datetime(2026, 11, 2, 11, 30, 0, tzinfo=timezone.utc) + timedelta(seconds=seconds)


def test_unsetFieldAlwaysWins():
    assert resolveWrite(ts(0), "portal", None, None, SKEW, PRIORITY) is True


def test_strictlyNewerWins():
    # incoming 10s after current, outside skew -> wins
    assert resolveWrite(ts(10), "portal", ts(0), "salesforce", SKEW, PRIORITY) is True


def test_strictlyOlderLoses():
    # incoming 10s before current, outside skew -> loses (out-of-order case)
    assert resolveWrite(ts(0), "salesforce", ts(10), "portal", SKEW, PRIORITY) is False


def test_tieDifferentSourceSalesforceWinsFromSalesforceSide():
    # equal timestamps, incoming salesforce vs current portal -> salesforce wins
    assert resolveWrite(ts(0), "salesforce", ts(0), "portal", SKEW, PRIORITY) is True


def test_tieDifferentSourceSalesforceWinsFromPortalSide():
    # equal timestamps, incoming portal vs current salesforce -> salesforce (current) stays
    assert resolveWrite(ts(0), "portal", ts(0), "salesforce", SKEW, PRIORITY) is False


def test_withinSkewTreatedAsTie():
    # incoming 1s newer than current but within the 2s skew window, portal vs salesforce
    # -> treated as a tie, salesforce wins, so the newer portal write LOSES.
    assert resolveWrite(ts(1), "portal", ts(0), "salesforce", SKEW, PRIORITY) is False


def test_tieSameSourceLaterWins():
    # same source within skew -> the later (incoming) write wins
    assert resolveWrite(ts(1), "portal", ts(0), "portal", SKEW, PRIORITY) is True


def test_isLoopEcho():
    assert isLoopEcho("integration@tequity.example", "integration@tequity.example") is True
    assert isLoopEcho("sales.user@client.example", "integration@tequity.example") is False
    assert isLoopEcho(None, "integration@tequity.example") is False


def test_diffFullRecordReturnsOnlyChanged():
    full = {"phone": "+91999", "city": "Noida", "email": "a@b.com"}
    current = {"phone": "+91999", "city": "Delhi", "email": "a@b.com"}
    assert diffFullRecord(full, current) == {"city": "Noida"}


def test_diffFullRecordNewFieldCounts():
    full = {"phone": "+91999", "owner": "ravi"}
    current = {"phone": "+91999"}
    assert diffFullRecord(full, current) == {"owner": "ravi"}


def test_asUtcAttachesUtcToNaive():
    naive = datetime(2026, 11, 2, 11, 30, 0)
    assert asUtc(naive).tzinfo is timezone.utc
