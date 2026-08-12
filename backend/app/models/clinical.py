"""Cuff readings, patient-reported events and the clinician summary.

Every table here is append-only (invariant 5). Corrections are new rows referencing the original,
never an UPDATE — see ``CuffReading.corrects_id``.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, RecordedAtMixin, SyntheticMixin, UuidPkMixin, utcnow
from app.models.core import enum_column
from app.models.enums import (
    CuffSource,
    HypertensionStatus,
    MedicationStatusToday,
    PregnancyAnswer,
    SexAtBirth,
)

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


class PatientContext(UuidPkMixin, SyntheticMixin, Base):
    """Clinical context the patient supplies about themselves.

    B2C PIVOT: with no clinic behind the account this is the only source of medication, pregnancy
    and rhythm history, and the only place a contraindication can be caught.

    **Append-only, latest-wins on read** (invariant 5). A changed answer inserts a new row; the
    previous one stays, because what the patient said in June is a fact about June and a session
    captured then should be read against the context in force at the time.
    """

    __tablename__ = "patient_context"

    patient_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("patient.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    recorded_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow
    )

    last_regimen_change_date: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )
    #: ``[{"name": ..., "dose": ...}]``. Bounded by the request schema, not by the column.
    medications: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb"), default=list
    )
    pregnant: Mapped[PregnancyAnswer] = mapped_column(
        enum_column(PregnancyAnswer, "pregnancy_answer"), nullable=False
    )
    known_arrhythmia: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)

    #: All three or none — a systolic with no date is not a reading. A CHECK holds them together.
    last_clinic_systolic_mmhg: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    last_clinic_diastolic_mmhg: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    last_clinic_taken_on: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        sa.CheckConstraint(
            "(last_clinic_systolic_mmhg IS NULL) = (last_clinic_diastolic_mmhg IS NULL) AND "
            "(last_clinic_systolic_mmhg IS NULL) = (last_clinic_taken_on IS NULL)",
            name="ck_patient_context_clinic_bp_all_or_nothing",
        ),
        sa.CheckConstraint(
            "last_clinic_systolic_mmhg IS NULL OR "
            "last_clinic_systolic_mmhg > last_clinic_diastolic_mmhg",
            name="ck_patient_context_clinic_bp_ordered",
        ),
        sa.Index("ix_patient_context_patient_recorded", "patient_id", "recorded_at"),
    )


class PhrProfile(UuidPkMixin, SyntheticMixin, Base):
    """The minimum viable PHR (PM spec section 28's ``phr_profiles``).

    **Deliberately mutable, and deliberately not a clinical record in invariant 5's sense.** It
    describes a person as they are now — a weight, a hypertension status — so a correction is a
    correction, not a new fact about a different moment. One row per patient, no append-only
    trigger, and `updated_at` carries the recency.

    The pregnancy and rhythm answers live in ``patient_context`` rather than here, because those
    *are* moment-in-time facts that the contraindication gate reads and must never be rewritten.
    """

    __tablename__ = "phr_profile"

    patient_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("patient.id", ondelete="RESTRICT"), nullable=False, unique=True
    )

    date_of_birth: Mapped[date | None] = mapped_column(sa.Date, nullable=True)
    sex_assigned_at_birth: Mapped[SexAtBirth | None] = mapped_column(
        enum_column(SexAtBirth, "sex_at_birth"), nullable=True
    )
    height_cm: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    weight_kg: Mapped[float | None] = mapped_column(sa.Float, nullable=True)

    hypertension_status: Mapped[HypertensionStatus | None] = mapped_column(
        enum_column(HypertensionStatus, "hypertension_status"), nullable=True
    )
    taking_bp_medication: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)

    #: Section 28's `health_conditions`, folded in. Codes are the spec's list.
    conditions: Mapped[list[str]] = mapped_column(
        ARRAY(sa.String(64)), nullable=False, server_default=sa.text("'{}'::varchar[]"), default=list
    )

    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        sa.CheckConstraint(
            "height_cm IS NULL OR (height_cm >= 50 AND height_cm <= 250)",
            name="ck_phr_profile_height_plausible",
        ),
        sa.CheckConstraint(
            "weight_kg IS NULL OR (weight_kg >= 10 AND weight_kg <= 400)",
            name="ck_phr_profile_weight_plausible",
        ),
        sa.UniqueConstraint("patient_id", name="uq_phr_profile_patient"),
    )


class SessionContext(UuidPkMixin, SyntheticMixin, Base):
    """CTX-01 for one check (PM spec section 28's ``session_context``).

    **Append-only** (invariant 5), unlike :class:`PhrProfile`. It records what the patient reported
    around one measurement at one moment; editing it later would rewrite what was true then. The
    spec's ``PATCH .../context`` is implemented as insert-and-supersede, and reads take the latest
    row.

    Filed here rather than as a ``symptom`` event, so the backend can tell a reported symptom from
    a context record without inspecting a payload.
    """

    __tablename__ = "session_context"

    session_id: Mapped[uuid.UUID] = mapped_column(
        sa.ForeignKey("measurement_session.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    recorded_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, default=utcnow
    )

    sleep_less_than_usual: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    stress_higher_than_usual: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    feeling_unwell: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    symptoms: Mapped[list[str]] = mapped_column(
        ARRAY(sa.String(64)), nullable=False, server_default=sa.text("'{}'::varchar[]"), default=list
    )
    medication_status_today: Mapped[MedicationStatusToday] = mapped_column(
        enum_column(MedicationStatusToday, "medication_status_today"), nullable=False
    )

    __table_args__ = (
        sa.Index("ix_session_context_session_recorded", "session_id", "recorded_at"),
    )
