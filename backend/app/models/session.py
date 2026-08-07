"""Measurement session, trend estimate and the ingest nonce."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SyntheticMixin, UuidPkMixin, utcnow
from app.models.core import enum_column
from app.models.enums import (
    DeviationState,
    Posture,
    RejectionReason,
    SessionStatus,
    TrendDirection,
)

#: Structural ceiling on the per-beat interval array, enforced as a database CHECK.
#:
#: Invariant 2: the deepest granularity accepted is one derived interval per beat, so this
#: column must not be usable to smuggle a waveform. The *operative* limit is
#: ``PlausibilitySettings.max_ptt_array_length``, which the API applies and which may be lowered
#: freely. Raising it above this ceiling requires a migration — deliberately, because widening
#: the channel that invariant 2 protects should not be an environment-variable change.
#: ``test_ptt_array_db_ceiling_matches_config`` fails if config drifts above it.
PTT_ARRAY_DB_CEILING = 300


class MeasurementSession(SyntheticMixin, Base):
    """One spot-check capture.

    The primary key is generated on the device and doubles as the idempotency key
    (BUILD_SPEC 4.2), so a retried upload cannot create a second row.

    ``ptt_ms`` holds one derived interval per beat and nothing deeper: no camera frames, no
    region-of-interest intensity series, no accelerometer samples (invariant 2).
    """

    __tablename__ = "measurement_session"

    #: Device-generated. Not server-defaulted — a session without a client id has no idempotency.
    id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, primary_key=True)

    episode_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("monitoring_episode.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    device_profile_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("device_profile.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: Null for calibration sessions — a capture taken before any baseline exists for this
    #: device still belongs in the record, it just cannot yield an estimate.
    calibration_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("calibration.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    model_version: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, index=True
    )
    posture: Mapped[Posture] = mapped_column(enum_column(Posture, "posture"), nullable=False)
    status: Mapped[SessionStatus] = mapped_column(
        enum_column(SessionStatus, "session_status"), nullable=False, index=True
    )
    rejection_reason: Mapped[RejectionReason | None] = mapped_column(
        enum_column(RejectionReason, "rejection_reason"), nullable=True
    )
    n_beats_total: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    n_beats_usable: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    ptt_ms: Mapped[list[float]] = mapped_column(
        ARRAY(sa.REAL), nullable=False, server_default=sa.text("'{}'::real[]")
    )
    #: Achieved rates and signal-quality metrics reported by the device gate. Validated against
    #: ``PlausibilitySettings.required_quality_fields`` before the row is written.
    quality: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow, server_default=sa.func.now()
    )

    estimate: Mapped["TrendEstimate | None"] = relationship(
        back_populates="session", uselist=False
    )
    episode: Mapped["MonitoringEpisode"] = relationship()  # noqa: F821 - resolved by registry

    __table_args__ = (
        # Invariant 3, verbatim from BUILD_SPEC 4.1. Status and reason cannot disagree.
        sa.CheckConstraint(
            "(status = 'rejected') = (rejection_reason IS NOT NULL)",
            name="ck_session_rejection_reason_matches_status",
        ),
        sa.CheckConstraint(
            "n_beats_usable <= n_beats_total", name="ck_session_usable_not_above_total"
        ),
        sa.CheckConstraint("n_beats_total >= 0", name="ck_session_beats_non_negative"),
        sa.CheckConstraint("n_beats_usable >= 0", name="ck_session_usable_non_negative"),
        # Invariant 2 — structural ceiling on the per-beat array.
        sa.CheckConstraint(
            f"ptt_ms IS NULL OR coalesce(array_length(ptt_ms, 1), 0) <= {PTT_ARRAY_DB_CEILING}",
            name="ck_session_ptt_array_length_bounded",
        ),
        # A one-dimensional array only; a nested array would be a waveform by another name.
        sa.CheckConstraint("ptt_ms IS NULL OR array_ndims(ptt_ms) <= 1", name="ck_session_ptt_1d"),
    )


class TrendEstimate(UuidPkMixin, SyntheticMixin, Base):
    """A direction and a magnitude in units of the patient's own baseline SD.

    Invariant 1: **there is no systolic or diastolic column here and there must never be one.**
    ``magnitude_sd`` is a count of standard deviations of this patient's own baseline. It is not
    a pressure, it does not convert to one, and no response derived from it may contain mmHg.
    ``test_trend_estimate_has_no_pressure_column`` introspects this table to keep it that way.
    """

    __tablename__ = "trend_estimate"

    #: One estimate per session, enforced by the unique constraint (BUILD_SPEC 4.1).
    session_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("measurement_session.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    )
    #: Invariant 4 — the calibration in force at the session's ``started_at``, not the one active
    #: now. Never null: an estimate without a calibration is uninterpretable, and where no
    #: calibration is in force no estimate is produced at all (invariant 7).
    calibration_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("calibration.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    direction: Mapped[TrendDirection] = mapped_column(
        enum_column(TrendDirection, "trend_direction"), nullable=False
    )
    magnitude_sd: Mapped[float] = mapped_column(sa.Float, nullable=False)
    confidence: Mapped[float] = mapped_column(sa.Float, nullable=False)
    #: BUILD_SPEC 4.3 — a single deviating session never triggers a cuff request, so whether a
    #: repeat within the window also deviated has to be recorded, not recomputed on read.
    deviation_state: Mapped[DeviationState] = mapped_column(
        enum_column(DeviationState, "deviation_state"),
        nullable=False,
        default=DeviationState.NONE,
    )
    computed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow, server_default=sa.func.now()
    )

    session: Mapped[MeasurementSession] = relationship(back_populates="estimate")

    __table_args__ = (
        sa.CheckConstraint("magnitude_sd >= 0", name="ck_estimate_magnitude_non_negative"),
        sa.CheckConstraint(
            "confidence > 0 AND confidence < 1", name="ck_estimate_confidence_open_unit_interval"
        ),
        # A stable estimate is by definition not a deviation; keeping these consistent at the
        # database level stops a caller constructing "stable but persistent".
        sa.CheckConstraint(
            "(direction = 'stable') = (deviation_state = 'none')",
            name="ck_estimate_direction_matches_deviation_state",
        ),
    )


class SessionNonce(UuidPkMixin, Base):
    """Single-use nonce with a TTL for session ingest (BUILD_SPEC 4.5).

    DEVIATION: not in the BUILD_SPEC 4.1 entity list, but 4.2/4.5 require single-use nonces and
    4.0 rules out Redis without justification. Storing them in Postgres keeps single-use
    semantics correct across multiple API processes, which an in-memory store would not.
    Not a clinical row: ``used_at`` is set on consumption.
    """

    __tablename__ = "session_nonce"

    value: Mapped[str] = mapped_column(sa.String(64), nullable=False, unique=True, index=True)
    #: The token subject the nonce was issued to. A nonce is not transferable between principals.
    issued_to: Mapped[str] = mapped_column(sa.String(128), nullable=False, index=True)
    issued_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow, server_default=sa.func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
