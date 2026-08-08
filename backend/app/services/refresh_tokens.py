"""Refresh-token rotation, revocation and reuse detection.

BUILD_SPEC 4.5 asks for "short-lived JWT access tokens plus refresh". Rotation is the part that
makes the refresh half safe: each use consumes the token and issues a new one, so a stolen token
is only good until the legitimate client next refreshes.

What makes that worth doing is what happens *after* the theft. If a token that has already been
rotated out is presented again, one of two things is true — either an attacker is replaying a
stolen token, or the legitimate client is. There is no way to tell which from the request, and
guessing wrong in the attacker's favour costs a patient their record. So the whole family is
revoked and both parties are forced to log in again. Annoying for the legitimate user, fatal for
the attacker; that asymmetry is the point.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models.auth import RefreshToken


class RefreshTokenError(Exception):
    """A refresh token that cannot be honoured. Maps to HTTP 401."""

    def __init__(self, message: str, *, family_revoked: bool = False) -> None:
        super().__init__(message)
        self.message = message
        #: True when the failure triggered a family-wide revocation, so the caller can log the
        #: incident rather than treating it as an ordinary expiry.
        self.family_revoked = family_revoked


@dataclass(frozen=True)
class IssuedRefreshToken:
    record: RefreshToken
    jti: str
    family_id: uuid.UUID


def issue(
    db: Session,
    *,
    user_id: uuid.UUID,
    expires_at: datetime,
    family_id: uuid.UUID | None = None,
) -> IssuedRefreshToken:
    """Record a newly minted refresh token.

    ``family_id`` is omitted for a fresh login and carried forward on rotation, so every token
    descended from one login shares it.
    """
    jti = uuid.uuid4().hex
    resolved_family = family_id or uuid.uuid4()

    record = RefreshToken(
        jti=jti,
        user_id=user_id,
        expires_at=expires_at,
        family_id=resolved_family,
    )
    db.add(record)
    db.flush()
    return IssuedRefreshToken(record=record, jti=jti, family_id=resolved_family)


def rotate(
    db: Session, *, jti: str, user_id: uuid.UUID, expires_at: datetime
) -> IssuedRefreshToken:
    """Consume ``jti`` and issue its replacement.

    Raises :class:`RefreshTokenError` if the token is unknown, expired, revoked, or has already
    been rotated. The last case revokes the whole family — see the module docstring.
    """
    record = db.execute(
        select(RefreshToken).where(RefreshToken.jti == jti).with_for_update()
    ).scalar_one_or_none()

    if record is None:
        # A validly signed token with no row: it was issued by a server whose database has since
        # been reset, or it is forged with a stolen signing key. Neither is a session to honour.
        raise RefreshTokenError("refresh token is not recognised")

    if record.user_id != user_id:
        # The signature verified but the subject disagrees with the record. Treat as hostile.
        revoke_family(db, family_id=record.family_id, reason="subject_mismatch")
        raise RefreshTokenError("refresh token does not belong to this subject", family_revoked=True)

    if record.revoked_at is not None:
        raise RefreshTokenError("refresh token has been revoked")

    if record.superseded_at is not None:
        # Reuse of a rotated token. Cannot distinguish attacker from victim, so end both.
        revoke_family(db, family_id=record.family_id, reason="refresh_token_reuse")
        raise RefreshTokenError(
            "refresh token has already been used; all sessions for this login have been ended",
            family_revoked=True,
        )

    now = datetime.now(tz=timezone.utc)
    if record.expires_at <= now:
        raise RefreshTokenError("refresh token has expired")

    replacement = issue(
        db, user_id=user_id, expires_at=expires_at, family_id=record.family_id
    )
    record.superseded_at = now
    record.replaced_by_id = replacement.record.id
    db.flush()
    return replacement


def revoke(db: Session, *, jti: str, reason: str) -> bool:
    """Revoke a single token. Returns False if it was not found or already revoked."""
    record = db.execute(
        select(RefreshToken).where(RefreshToken.jti == jti).with_for_update()
    ).scalar_one_or_none()

    if record is None or record.revoked_at is not None:
        return False

    record.revoked_at = datetime.now(tz=timezone.utc)
    record.revoked_reason = reason
    db.flush()
    return True


def revoke_family(db: Session, *, family_id: uuid.UUID, reason: str) -> int:
    """Revoke every unrevoked token descended from one login. Returns how many were ended."""
    result = db.execute(
        update(RefreshToken)
        .where(RefreshToken.family_id == family_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(tz=timezone.utc), revoked_reason=reason)
    )
    db.flush()
    return result.rowcount or 0


def revoke_all_for_user(db: Session, *, user_id: uuid.UUID, reason: str) -> int:
    """Revoke every active refresh token a user holds. Used by logout-everywhere."""
    result = db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=datetime.now(tz=timezone.utc), revoked_reason=reason)
    )
    db.flush()
    return result.rowcount or 0


def active_session_count(db: Session, *, user_id: uuid.UUID) -> int:
    """How many refresh tokens the user could still present. Surfaced by ``GET /v1/auth/me``."""
    rows = db.execute(
        select(RefreshToken.id).where(
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.superseded_at.is_(None),
            RefreshToken.expires_at > datetime.now(tz=timezone.utc),
        )
    ).all()
    return len(rows)
