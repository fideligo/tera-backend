"""Device profile and calibration.

Invariant 4: calibration is versioned and device-bound. Every estimate references the calibration
in force at capture time; every calibration references a device profile; at most one calibration
is active per patient per device; recalibration inserts a new row and supersedes the old one.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, SyntheticMixin, UuidPkMixin, utcnow
from app.models.core import enum_column
from app.models.enums import (
    CalibrationStatus,
    CameraHardwareLevel,
    QualifiedStatus,
    TimestampSource,
)


class DeviceProfile(UuidPkMixin, SyntheticMixin, Base):
    """A handset's measured capability and its eligibility verdict.

    The numbers here are *measured* by the Phase 3 profiler, never estimated (invariant 9). The
    verdict is derived from them by ``app.services.eligibility`` using the configured bands.
    """

    __tablename__ = "device_profile"

    patient_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("patient.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    model: Mapped[str] = mapped_column(sa.String(128), nullable=False)
    os_version: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    accel_rate_hz: Mapped[float] = mapped_column(sa.Float, nullable=False)
    camera_fps: Mapped[float] = mapped_column(sa.Float, nullable=False)
    camera_hw_level: Mapped[CameraHardwareLevel] = mapped_column(
        enum_column(CameraHardwareLevel, "camera_hardware_level"), nullable=False
    )
    manual_sensor: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    timestamp_source: Mapped[TimestampSource] = mapped_column(
        enum_column(TimestampSource, "timestamp_source"), nullable=False
    )
    clock_offset_sd_ms: Mapped[float] = mapped_column(sa.Float, nullable=False)
    qualified_status: Mapped[QualifiedStatus] = mapped_column(
        enum_column(QualifiedStatus, "qualified_status"), nullable=False
    )
    submitted_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow, server_default=sa.func.now()
    )

    __table_args__ = (
        sa.CheckConstraint("accel_rate_hz > 0", name="ck_device_profile_accel_rate_positive"),
        sa.CheckConstraint("camera_fps > 0", name="ck_device_profile_camera_fps_positive"),
        sa.CheckConstraint(
            "clock_offset_sd_ms >= 0", name="ck_device_profile_clock_offset_sd_non_negative"
        ),
    )


class Calibration(UuidPkMixin, SyntheticMixin, Base):
    """A patient's personal PTT baseline, bound to one device profile.

    ``established_at`` and ``superseded_at`` are a DEVIATION from the column list in BUILD_SPEC
    4.1. Without them "the calibration in force at capture time" (invariant 4) cannot be
    resolved: a recalibration between capture and upload would silently re-point an estimate at
    a baseline that did not exist when the session was recorded. See docs/decisions.md.
    """

    __tablename__ = "calibration"

    patient_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("patient.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    device_profile_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("device_profile.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    #: The cuff reading that anchors this baseline. A PTT baseline without a cuff reference is
    #: not a calibration — it is an uninterpreted number.
    reference_cuff_reading_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("cuff_reading.id", ondelete="RESTRICT"), nullable=False
    )
    baseline_mean_ms: Mapped[float] = mapped_column(sa.Float, nullable=False)
    baseline_sd_ms: Mapped[float] = mapped_column(sa.Float, nullable=False)
    n_sessions: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    status: Mapped[CalibrationStatus] = mapped_column(
        enum_column(CalibrationStatus, "calibration_status"),
        nullable=False,
        default=CalibrationStatus.ACTIVE,
    )
    #: DEFERRABLE INITIALLY DEFERRED, and it has to be. Supersession must mark the old row
    #: superseded *before* the new row is inserted, or the partial unique index below sees two
    #: active calibrations for the same patient and device and rejects the insert. That means
    #: the old row briefly points at an id that does not exist yet, so the FK check has to wait
    #: until commit.
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey(
            "calibration.id",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
            name="fk_calibration_superseded_by_id",
        ),
        nullable=True,
    )
    established_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow, server_default=sa.func.now(),
        index=True,
    )
    superseded_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    superseded_by: Mapped["Calibration | None"] = relationship(
        remote_side="Calibration.id", foreign_keys=[superseded_by_id]
    )
    source_sessions: Mapped[list["CalibrationSourceSession"]] = relationship(
        back_populates="calibration", cascade="all"
    )

    __table_args__ = (
        # BUILD_SPEC 4.1 — both required at database level.
        sa.CheckConstraint("baseline_sd_ms > 0", name="ck_calibration_baseline_sd_positive"),
        sa.CheckConstraint("n_sessions >= 3", name="ck_calibration_min_sessions"),
        # Supersession bookkeeping must be internally consistent: a superseded row names its
        # successor and when it happened; an active row does neither.
        sa.CheckConstraint(
            "(status = 'superseded') = (superseded_by_id IS NOT NULL)",
            name="ck_calibration_superseded_by_matches_status",
        ),
        sa.CheckConstraint(
            "(status = 'superseded') = (superseded_at IS NOT NULL)",
            name="ck_calibration_superseded_at_matches_status",
        ),
        sa.CheckConstraint(
            "superseded_by_id IS NULL OR superseded_by_id <> id",
            name="ck_calibration_not_self_superseding",
        ),
        # Invariant 4 — at most one active calibration per patient per device.
        sa.Index(
            "uq_calibration_one_active_per_patient_device",
            "patient_id",
            "device_profile_id",
            unique=True,
            postgresql_where=sa.text("status = 'active'"),
        ),
    )


class CalibrationSourceSession(Base):
    """Which accepted sessions produced a calibration's baseline.

    DEVIATION from BUILD_SPEC 4.1, which records only ``n_sessions``. Provenance is needed for
    two reasons: ``n_sessions >= 3`` is a database CHECK, and without knowing which sessions were
    used the server would have to trust a client-supplied count and baseline — which BUILD_SPEC
    4.4 explicitly forbids elsewhere. With this table the server computes the baseline itself.
    """

    __tablename__ = "calibration_source_session"

    calibration_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("calibration.id", ondelete="RESTRICT"), primary_key=True
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("measurement_session.id", ondelete="RESTRICT"), primary_key=True
    )
    #: The trimmed-mean session PTT that this session contributed, recorded so the baseline can
    #: be audited without recomputing from the per-beat array.
    session_ptt_ms: Mapped[float] = mapped_column(sa.Float, nullable=False)

    calibration: Mapped[Calibration] = relationship(back_populates="source_sessions")
