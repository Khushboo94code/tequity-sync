"""Shared pytest fixtures: a throwaway file-backed SQLite database per test.

A file (not :memory:) is used so the worker/restart tests can open the same database from a
background thread and across simulated process restarts.
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import models  # noqa: F401  (registers models on Base.metadata)
from app.db import Base


@pytest.fixture
def dbUrl(tmp_path):
    """A unique SQLite URL per test."""
    return f"sqlite:///{tmp_path}/test.db"


@pytest.fixture
def dbEngine(dbUrl):
    """An engine with the schema created."""
    engine = create_engine(dbUrl, connect_args={"check_same_thread": False}, future=True)
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def makeSession(dbEngine):
    """A session factory bound to the test engine (for tests needing multiple sessions)."""
    return sessionmaker(bind=dbEngine, expire_on_commit=False, future=True)


@pytest.fixture
def session(makeSession):
    """A single open session for simple tests."""
    s = makeSession()
    try:
        yield s
    finally:
        s.close()
