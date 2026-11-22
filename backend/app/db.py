"""Database engine, session factory, and schema creation.

Synchronous SQLAlchemy is used deliberately: fewer moving parts than async, and the
primitives map cleanly onto the design's one-transaction-per-event model. SQLite is the
default (frictionless demo and tests); Postgres is used via DATABASE_URL in docker-compose.
"""
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import config


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


def makeEngine(databaseUrl: str | None = None):
    """Build a SQLAlchemy engine for the given URL (defaults to config.databaseUrl).

    SQLite needs check_same_thread=False because the worker runs on a background thread
    while FastAPI serves requests on others.
    """
    url = databaseUrl or config.databaseUrl
    connectArgs = {"check_same_thread": False} if url.startswith("sqlite") else {}
    return create_engine(url, connect_args=connectArgs, future=True)


engine = makeEngine()
SessionLocal = sessionmaker(
    bind=engine, autoflush=False, expire_on_commit=False, future=True
)


def initDb(targetEngine=None) -> None:
    """Create all tables if absent. Idempotent; safe on every startup and in tests."""
    from . import models  # noqa: F401  (import registers the models on Base.metadata)

    Base.metadata.create_all(targetEngine or engine)
