"""Single-use nonces for session ingest (BUILD_SPEC 4.5).

Stored in Postgres rather than in process memory. The single-use property has to hold across
every API process, and an in-memory store would let the same nonce be spent once per worker.
BUILD_SPEC rules out Redis without justification, and this needs no second datastore.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import SecuritySettings
from app.models import SessionNonce

#: 32 bytes of urandom, hex-encoded. Guessing one is not a realistic attack path; the nonce
#: exists to stop replay of a captured request, not to be a secret in its own right.
_NONCE_BYTES = 32


class NonceError(Exception):
    """Raised when a nonce is absent, expired or already used. Maps to HTTP 428."""


def issue_nonce(
    session: Session, *, issued_to: str, settings: SecuritySettings, synthetic: bool = False
) -> tuple[str, datetime]:
    """Mint a nonce for ``issued_to`` and return it with its expiry."""
    del synthetic  # nonces are not clinical rows and carry no synthetic flag
    value = secrets.token_hex(_NONCE_BYTES)
    expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=settings.nonce_ttl_seconds)
    session.add(SessionNonce(value=value, issued_to=issued_to, expires_at=expires_at))
    session.flush()
    return value, expires_at


def consume_nonce(session: Session, *, value: str | None, presented_by: str) -> None:
    """Spend a nonce, or raise ``NonceError``.

    The row is locked FOR UPDATE before the used check, so two concurrent submissions of the
    same nonce cannot both pass — one of them will block, then find it spent.
    """
    if not value:
        raise NonceError("X-Session-Nonce header is required")

    row = session.execute(
        select(SessionNonce).where(SessionNonce.value == value).with_for_update()
    ).scalar_one_or_none()

    if row is None:
        raise NonceError("nonce is not recognised")
    if row.issued_to != presented_by:
        # Not transferable between principals: a nonce leaked from one handset must not let
        # another submit sessions.
        raise NonceError("nonce was not issued to this caller")
    if row.used_at is not None:
        raise NonceError("nonce has already been used")

    now = datetime.now(tz=timezone.utc)
    if row.expires_at <= now:
        raise NonceError("nonce has expired")

    row.used_at = now
    session.flush()


def purge_expired(session: Session) -> int:
    """Delete spent and expired nonces. Housekeeping only — not a clinical table."""
    now = datetime.now(tz=timezone.utc)
    stale = session.execute(
        select(SessionNonce).where(SessionNonce.expires_at < now)
    ).scalars().all()
    for row in stale:
        session.delete(row)
    return len(stale)
