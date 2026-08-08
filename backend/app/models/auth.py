"""Refresh-token records.

A JWT is self-contained, which is exactly the problem: once issued it stays valid until it
expires, and nothing on the server can take it back. For access tokens that is acceptable
because their lifetime is 15 minutes. For a refresh token living 14 days it is not — a leaked
one is a 14-day session on a patient's health record with no way to end it.

So every refresh token has a row here, keyed by its ``jti``. The token is only honoured if its
row exists and is neither revoked nor superseded. That makes logout real, and makes the reuse
detection below possible.

The table is deliberately *not* in ``CLINICAL_TABLES``: it holds no clinical content, and
revocation requires updating rows, which the append-only trigger would forbid.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, UuidPkMixin, utcnow


class RefreshToken(Base, UuidPkMixin):
    """One issued refresh token.

    Rotation means each refresh mints a new row and marks the old one ``superseded``, linked by
    :attr:`replaced_by_id`. The chain that results is what makes theft detectable.
    """

    __tablename__ = "refresh_token"

    #: The JWT's ``jti`` claim. Unique, because two tokens sharing one would make revocation
    #: ambiguous at exactly the moment it matters.
    jti: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True, index=True)

    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=False, index=True
    )

    issued_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow, server_default=sa.func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    #: Set when the token is deliberately ended — logout, or a chain revoked after reuse.
    revoked_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    #: Why it was revoked. Free text, read by an operator investigating an incident; never
    #: contains clinical content.
    revoked_reason: Mapped[str | None] = mapped_column(sa.String(64), nullable=True)

    #: Set when this token was rotated out normally. Distinct from revocation: a superseded
    #: token was used correctly and replaced, a revoked one was ended.
    superseded_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    replaced_by_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("refresh_token.id", ondelete="RESTRICT"), nullable=True
    )

    #: Grouping key for one login. Every token rotated from the same original login shares it,
    #: so revoking a compromised chain does not require walking the ``replaced_by`` links.
    family_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)

    user = relationship("AppUser")
    replaced_by: Mapped["RefreshToken | None"] = relationship(
        remote_side="RefreshToken.id", foreign_keys=[replaced_by_id]
    )

    __table_args__ = (
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_reason IS NOT NULL",
            name="ck_refresh_token_revoked_has_reason",
        ),
        sa.CheckConstraint(
            "(superseded_at IS NULL) = (replaced_by_id IS NULL)",
            name="ck_refresh_token_superseded_has_successor",
        ),
        sa.CheckConstraint(
            "replaced_by_id IS NULL OR replaced_by_id <> id",
            name="ck_refresh_token_not_self_replacing",
        ),
        sa.Index("ix_refresh_token_active", "user_id", "revoked_at", "superseded_at"),
    )

    @property
    def is_active(self) -> bool:
        """Usable right now: not revoked, not already rotated out."""
        return self.revoked_at is None and self.superseded_at is None
