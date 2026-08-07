"""Patient, episode, user and audit tables."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SyntheticMixin, UuidPkMixin, utcnow
from app.models.enums import AuditAction, UserRole


def enum_column(python_enum: type, name: str) -> sa.Enum:
    """Build a Postgres native enum column type storing the enum's *values*."""
    return sa.Enum(
        python_enum,
        name=name,
        native_enum=True,
        values_callable=lambda enum_cls: [member.value for member in enum_cls],
    )


class Patient(UuidPkMixin, SyntheticMixin, Base):
    """A pseudonymous patient.

    BUILD_SPEC 4.1: "pseudonymous id, clinic id, enrolled_at. No name or contact fields." There
    is deliberately nowhere to put a name — if identity is ever needed it belongs in the clinic's
    own system, keyed by ``pseudonym``.
    """

    __tablename__ = "patient"

    pseudonym: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True)
    clinic_id: Mapped[str] = mapped_column(sa.String(64), nullable=False, index=True)
    enrolled_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    episodes: Mapped[list["MonitoringEpisode"]] = relationship(back_populates="patient")


class AppUser(UuidPkMixin, SyntheticMixin, Base):
    """An authenticating principal.

    DEVIATION from BUILD_SPEC 4.1, which lists no user entity: 4.5 requires role claims and
    clinician access "scoped to episodes where they are the reviewing professional", which is not
    implementable without somewhere to hold the clinician identity. See docs/decisions.md.
    """

    __tablename__ = "app_user"

    subject: Mapped[str] = mapped_column(sa.String(128), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(sa.String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(enum_column(UserRole, "user_role"), nullable=False)
    clinic_id: Mapped[str | None] = mapped_column(sa.String(64), nullable=True, index=True)
    # Set only for role='patient'; ties the token subject to exactly one patient record.
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("patient.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow, server_default=sa.func.now()
    )

    patient: Mapped[Patient | None] = relationship()

    __table_args__ = (
        # A patient principal must point at a patient; a clinician or admin must not.
        sa.CheckConstraint(
            "(role = 'patient') = (patient_id IS NOT NULL)",
            name="ck_app_user_patient_link_matches_role",
        ),
    )


class MonitoringEpisode(UuidPkMixin, SyntheticMixin, Base):
    """A 4-8 week monitoring window opened when treatment is adjusted."""

    __tablename__ = "monitoring_episode"

    patient_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("patient.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # DEVIATION from BUILD_SPEC 4.1: required to scope clinician access (4.5).
    reviewing_clinician_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("app_user.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    started_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    #: Per-episode clinical configuration (invariant 10). Keys, all optional, falling back to
    #: app.config defaults: ``cuff_schedule`` (free-form description plus interval_days),
    #: ``deviation_k``, ``min_beat_count``, ``persistence_window_hours``.
    protocol_params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"), default=dict
    )

    patient: Mapped[Patient] = relationship(back_populates="episodes")
    reviewing_clinician: Mapped[AppUser | None] = relationship()

    __table_args__ = (
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at > started_at", name="ck_episode_end_after_start"
        ),
    )


class AuditLog(UuidPkMixin, Base):
    """Append-only audit trail (invariant 5).

    UPDATE and DELETE are blocked by a database trigger, not merely by the absence of a route —
    an append-only log that the application layer can rewrite is not append-only.
    """

    __tablename__ = "audit_log"

    actor: Mapped[str] = mapped_column(sa.String(128), nullable=False, index=True)
    role: Mapped[UserRole] = mapped_column(enum_column(UserRole, "user_role"), nullable=False)
    action: Mapped[AuditAction] = mapped_column(
        enum_column(AuditAction, "audit_action"), nullable=False, index=True
    )
    #: Opaque identifier of the affected row. Never clinical content.
    target: Mapped[str | None] = mapped_column(sa.String(128), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow, server_default=sa.func.now(),
        index=True,
    )
