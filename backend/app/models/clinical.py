"""Cuff readings, patient-reported events and the clinician summary.

Every table here is append-only (invariant 5). Corrections are new rows referencing the original,
never an UPDATE — see ``CuffReading.corrects_id``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, RecordedAtMixin, SyntheticMixin, UuidPkMixin, utcnow
from app.models.core import enum_column
from app.models.enums import CuffSource

# Plausibility ranges are given verbatim in BUILD_SPEC 4.1 and must exist at database level.
# They mirror PlausibilitySettings; the config values are what the API rejects on, these are the
# structural backstop. ``test_cuff_plausibility_db_matches_config`` keeps the two aligned.
SYSTOLIC_MIN_MMHG, SYSTOLIC_MAX_MMHG = 50, 300
DIASTOLIC_MIN_MMHG, DIASTOLIC_MAX_MMHG = 30, 200
PULSE_MIN_BPM, PULSE_MAX_BPM = 25, 250


class CuffReading(UuidPkMixin, SyntheticMixin, RecordedAtMixin, Base):
    """A validated upper-arm cuff measurement.

    Invariant 1: **this is the only table in the system that holds mmHg.** Every pressure value
    a patient or clinician ever sees originates here, from a cuff, confirmed by a person.
    """

    __tablename__ = "cuff_reading"

    episode_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("monitoring_episode.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    systolic_mmhg: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    diastolic_mmhg: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    pulse_bpm: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    source: Mapped[CuffSource] = mapped_column(
        enum_column(CuffSource, "cuff_source"), nullable=False
    )
    #: Only meaningful for ``source = 'photograph'``, which the API currently rejects because
    #: seven-segment OCR is out of scope (BUILD_SPEC 8).
    ocr_confidence: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    taken_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, index=True
    )
    #: NOT NULL by BUILD_SPEC 4.1: a reading nobody confirmed is not a reading. Even an OCR
    #: result requires a human to confirm the digits before it enters the record.
    user_confirmed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False
    )
    #: Invariant 5 — a correction is a new row pointing at the row it corrects. The original is
    #: never edited and never disappears.
    corrects_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.ForeignKey("cuff_reading.id", ondelete="RESTRICT"), nullable=True
    )

    __table_args__ = (
        sa.CheckConstraint(
            f"systolic_mmhg BETWEEN {SYSTOLIC_MIN_MMHG} AND {SYSTOLIC_MAX_MMHG}",
            name="ck_cuff_systolic_plausible",
        ),
        sa.CheckConstraint(
            f"diastolic_mmhg BETWEEN {DIASTOLIC_MIN_MMHG} AND {DIASTOLIC_MAX_MMHG}",
            name="ck_cuff_diastolic_plausible",
        ),
        sa.CheckConstraint(
            f"pulse_bpm IS NULL OR pulse_bpm BETWEEN {PULSE_MIN_BPM} AND {PULSE_MAX_BPM}",
            name="ck_cuff_pulse_plausible",
        ),
        sa.CheckConstraint(
            "systolic_mmhg > diastolic_mmhg", name="ck_cuff_systolic_above_diastolic"
        ),
        sa.CheckConstraint(
            "(source = 'photograph') OR (ocr_confidence IS NULL)",
            name="ck_cuff_ocr_confidence_only_for_photograph",
        ),
        sa.CheckConstraint("corrects_id IS NULL OR corrects_id <> id", name="ck_cuff_not_self_correcting"),
    )


class MedicationEvent(UuidPkMixin, SyntheticMixin, RecordedAtMixin, Base):
    """A medication-taken report.

    Invariant 6: this records what the patient says they did. Nothing in the system responds to
    it with advice, and no endpoint suggests a change.
    """

    __tablename__ = "medication_event"

    episode_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("monitoring_episode.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    occurred_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class SymptomEvent(UuidPkMixin, SyntheticMixin, RecordedAtMixin, Base):
    """A non-red-flag symptom report."""

    __tablename__ = "symptom_event"

    episode_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("monitoring_episode.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    occurred_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class RedFlagEvent(UuidPkMixin, SyntheticMixin, RecordedAtMixin, Base):
    """A red-flag symptom report (invariant 8).

    The row is a *record* of an escalation the handset has already performed locally. The
    emergency instruction must not depend on this insert succeeding, or on the network being
    available at all.
    """

    __tablename__ = "red_flag_event"

    episode_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("monitoring_episode.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    occurred_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, index=True
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)


class ClinicianSummary(UuidPkMixin, SyntheticMixin, Base):
    """A generated exception summary for one episode.

    Append-only: each generation inserts a row rather than updating the last one, so the record
    shows what a clinician actually saw and when, not just the latest rendering.
    """

    __tablename__ = "clinician_summary"

    episode_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("monitoring_episode.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    generated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow, server_default=sa.func.now()
    )
    #: Notification delivery is out of scope (BUILD_SPEC 8), so this stays null for now; the
    #: column exists because the spec lists it.
    delivered_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    viewed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    contents: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
