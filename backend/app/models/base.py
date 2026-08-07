"""Declarative base and shared column mixins."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Timezone-aware UTC now. All stored timestamps are UTC."""
    return datetime.now(tz=timezone.utc)


class Base(DeclarativeBase):
    """Declarative base for every Tera table."""


class UuidPkMixin:
    """UUID primary key generated server-side."""

    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, primary_key=True, default=uuid.uuid4
    )


class SyntheticMixin:
    """Invariant 9 — no fabricated data presented as real.

    Every clinical table carries this flag, it defaults to false, and every API response that
    exposes the row surfaces it. Seeded rows set it true. A row that is synthetic must be
    impossible to mistake for a real one at any layer.
    """

    synthetic: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.false(), default=False, index=True
    )


class RecordedAtMixin:
    """When the server persisted the row, as distinct from when the event occurred."""

    recorded_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), default=utcnow
    )
