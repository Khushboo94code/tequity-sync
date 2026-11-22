"""Portal authentication: password hashing, session tokens, and the currentUser dependency.

Deliberately demo-grade and stdlib-only (no bcrypt/JWT dependency): pbkdf2_hmac password hashing
with a per-user salt, and opaque bearer tokens persisted in the sessions table. The point is to make
the edit actor REAL — derived from the authenticated session on the server, never trusted from the
request body — which is the attribution principle the design argues for (see docs/DECISIONS.md).

Hardening (password policy, token expiry, rate limiting, HTTPS) is out of scope for the exercise.
"""
import hashlib
import hmac
import secrets

from fastapi import Header, HTTPException

from . import db
from .models import AuthSession, User

_PBKDF2_ROUNDS = 200_000


def hashPassword(password: str) -> str:
    """Return 'salt$hash' using pbkdf2_hmac(sha256) with a fresh random per-user salt."""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ROUNDS).hex()
    return f"{salt}${digest}"


def verifyPassword(password: str, stored: str) -> bool:
    """Constant-time verification of a password against a stored 'salt$hash'."""
    try:
        salt, digest = stored.split("$", 1)
    except ValueError:
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _PBKDF2_ROUNDS).hex()
    return hmac.compare_digest(check, digest)


def createSession(session, email: str) -> str:
    """Create and persist an opaque bearer token for a user; return the token."""
    token = secrets.token_urlsafe(32)
    session.add(AuthSession(token=token, email=email))
    session.commit()
    return token


def resolveToken(token: str) -> str | None:
    """Return the email a token belongs to, or None if the token is unknown."""
    # Uses db.SessionLocal via the module so tests that inject a throwaway engine are honoured.
    s = db.SessionLocal()
    try:
        row = s.get(AuthSession, token)
        return row.email if row else None
    finally:
        s.close()


def currentUser(authorization: str | None = Header(default=None)) -> str:
    """FastAPI dependency: validate the Bearer token and return the user's email.

    This is how a protected endpoint learns WHO is acting — server-side, from the session — so a
    portal edit is attributed to the authenticated user rather than to anything the client sends.
    Raises 401 when the header is missing/malformed or the token is unknown.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1].strip()
    email = resolveToken(token)
    if email is None:
        raise HTTPException(status_code=401, detail="invalid or expired token")
    return email
