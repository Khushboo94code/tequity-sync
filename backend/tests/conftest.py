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


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A FastAPI TestClient wired to a throwaway database (never touches the dev DB).

    Injects a per-test SQLite engine into app.db / app.main / the worker, so the whole HTTP app
    (lifespan, auth, worker) runs against an isolated database.
    """
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.db as db
    import app.main as main

    engine = create_engine(
        f"sqlite:///{tmp_path}/api.db", connect_args={"check_same_thread": False}, future=True
    )
    Sess = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", Sess)
    monkeypatch.setattr(main, "SessionLocal", Sess)
    monkeypatch.setattr(main.worker, "makeSession", Sess)
    with TestClient(main.app) as c:
        yield c


@pytest.fixture
def authHeaders(client):
    """Sign up a default portal user and return its Bearer auth header."""
    res = client.post("/auth/signup", json={"email": "tester@client.example", "password": "pw"})
    assert res.status_code == 200
    return {"Authorization": f"Bearer {res.json()['token']}"}
