"""SQLAlchemy models — the five tables from DESIGN.md section 4.

- fieldState   : current value of each (record, field) — the value we can justify.
- fieldHistory : append-only audit of every accepted write (winner or not).
- inbox        : eventId-keyed idempotency + inbound durability (duplicate defense).
- outbox       : durable outbound queue (survives restart; crosses the boundary).
- syncCursor   : high-water mark per objectType for the post-outage backfill pull.

Column names are camelCase to match the wire format; see docs/DECISIONS.md.
"""
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


def utcNow() -> datetime:
    """Timezone-aware current UTC time. Used for all server-generated timestamps."""
    return datetime.now(timezone.utc)


# BigInteger on Postgres, Integer on SQLite (SQLite autoincrement needs INTEGER PK).
_AutoId = BigInteger().with_variant(Integer, "sqlite")


class FieldState(Base):
    """The current value of one field of one record — the 'one value we can justify'."""

    __tablename__ = "fieldState"

    recordId: Mapped[str] = mapped_column(String, primary_key=True)
    fieldName: Mapped[str] = mapped_column(String, primary_key=True)
    objectType: Mapped[str] = mapped_column(String)
    currentValue: Mapped[str | None] = mapped_column(Text, nullable=True)
    currentTs: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    currentActor: Mapped[str] = mapped_column(String)
    currentSource: Mapped[str] = mapped_column(String)


class FieldHistory(Base):
    """Append-only record of every accepted write. Never updated or deleted."""

    __tablename__ = "fieldHistory"

    id: Mapped[int] = mapped_column(_AutoId, primary_key=True, autoincrement=True)
    recordId: Mapped[str] = mapped_column(String, index=True)
    fieldName: Mapped[str] = mapped_column(String, index=True)
    objectType: Mapped[str] = mapped_column(String)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor: Mapped[str] = mapped_column(String)
    source: Mapped[str] = mapped_column(String)
    eventId: Mapped[str | None] = mapped_column(String, nullable=True)
    appliedCurrent: Mapped[bool] = mapped_column(Boolean)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcNow)


class Inbox(Base):
    """Idempotency + inbound durability. The eventId PK is the duplicate-webhook defense."""

    __tablename__ = "inbox"

    eventId: Mapped[str] = mapped_column(String, primary_key=True)
    recordId: Mapped[str] = mapped_column(String)
    objectType: Mapped[str] = mapped_column(String)
    receivedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcNow)
    status: Mapped[str] = mapped_column(String, default="processed")
    payload: Mapped[dict] = mapped_column(JSON)


class Outbox(Base):
    """Durable outbound queue. A portal edit is recorded here in the same TX that updates
    fieldState, never sent inline — which is why a restart cannot lose a pending call."""

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(_AutoId, primary_key=True, autoincrement=True)
    recordId: Mapped[str] = mapped_column(String, index=True)
    objectType: Mapped[str] = mapped_column(String)
    fieldName: Mapped[str] = mapped_column(String)
    newValue: Mapped[str | None] = mapped_column(Text, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    nextAttemptAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcNow)
    lastError: Mapped[str | None] = mapped_column(Text, nullable=True)
    createdAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcNow)


class SyncCursor(Base):
    """High-water mark (per objectType) for the SystemModstamp backfill pull after an outage."""

    __tablename__ = "syncCursor"

    objectType: Mapped[str] = mapped_column(String, primary_key=True)
    lastSystemModstamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updatedAt: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcNow)
