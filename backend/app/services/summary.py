"""Assemble the clinician exception summary.

BUILD_SPEC 5.3: scannable in under two minutes, because the evidence says clinicians have very
little consultation time. So it reports departures from expectation, and it reports them as
facts.

Invariant 6 is the binding constraint here: **nothing in this document interprets what the
findings mean clinically.** It says what was measured, what was reported, what the device could
not do, and what the calibration state is. The clinical judgement is the clinician's.
"""

from __future__ import annotations

import uuid
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Calibration,
    ClinicianSummary,
    CuffReading,
    DeviationState,
    DeviceProfile,
    MeasurementSession,
    MedicationEvent,
    MonitoringEpisode,
    RedFlagEvent,
    SessionStatus,
    SymptomEvent,
    TrendEstimate,
)
from app.schemas.common import SyntheticFlag
from app.schemas.summary import (
    ClinicianSummaryOut,
    EpisodeListItem,
    SummaryCalibrationVersion,
    SummaryCuffReading,
    SummaryEvent,
    SummaryMedicationLog,
    SummaryNotableChange,
    SummaryRejectedSession,
    SummarySessionYield,
)
from app.services import language

#: A session uploaded more than this long after capture is flagged as unsynchronised. It is a
#: system indicator (the handset was offline or the app was not opened), never a clinical one.
UNSYNCHRONISED_THRESHOLD_HOURS = 24


def build(
    db: Session, *, episode: MonitoringEpisode, now: datetime | None = None
) -> ClinicianSummaryOut:
    """Build the summary document for one episode."""
    now = now or datetime.now(tz=timezone.utc)

    cuff_rows = (
        db.execute(
            select(CuffReading)
            .where(CuffReading.episode_id == episode.id)
            .order_by(CuffReading.taken_at.desc())
        )
        .scalars()
        .all()
    )
    sessions = (
        db.execute(
            select(MeasurementSession)
            .where(MeasurementSession.episode_id == episode.id)
            .order_by(MeasurementSession.started_at.desc())
        )
        .scalars()
        .all()
    )
    estimate_rows = db.execute(
        select(TrendEstimate, MeasurementSession)
        .join(MeasurementSession, MeasurementSession.id == TrendEstimate.session_id)
        .where(MeasurementSession.episode_id == episode.id)
        .order_by(MeasurementSession.started_at.desc())
    ).all()

    rejected = [s for s in sessions if s.status is SessionStatus.REJECTED]
    completed = [s for s in sessions if s.status is SessionStatus.COMPLETED]

    contains_synthetic = episode.synthetic or any(
        row.synthetic for row in [*cuff_rows, *sessions]
    )

    return ClinicianSummaryOut(
        episode_id=episode.id,
        patient_pseudonym=episode.patient.pseudonym,
        clinic_id=episode.patient.clinic_id,
        started_at=episode.started_at,
        ended_at=episode.ended_at,
        generated_at=now,
        protocol_params=episode.protocol_params,
        synthetic=contains_synthetic,
        synthetic_notice=SyntheticFlag.notice_for(contains_synthetic),
        cuff_readings=[
            SummaryCuffReading(
                id=row.id,
                systolic_mmhg=row.systolic_mmhg,
                diastolic_mmhg=row.diastolic_mmhg,
                pulse_bpm=row.pulse_bpm,
                taken_at=row.taken_at,
                user_confirmed_at=row.user_confirmed_at,
                corrects_id=row.corrects_id,
                synthetic=row.synthetic,
            )
            for row in cuff_rows
        ],
        # Exception-based: only estimates that departed from baseline are listed. The rest are
        # in the timeline; putting all thirty here would defeat the two-minute scan.
        notable_changes=[
            SummaryNotableChange(
                session_id=session_row.id,
                occurred_at=session_row.started_at,
                direction=estimate.direction,
                magnitude_sd=estimate.magnitude_sd,
                confidence=estimate.confidence,
                deviation_state=estimate.deviation_state,
                cuff_requested=estimate.deviation_state is DeviationState.PERSISTENT,
                synthetic=estimate.synthetic,
            )
            for estimate, session_row in estimate_rows
            if estimate.deviation_state is not DeviationState.NONE
        ],
        # Invariant 3 — reported, with reasons, never hidden.
        rejected_sessions=[
            SummaryRejectedSession(
                session_id=row.id,
                occurred_at=row.started_at,
                rejection_reason=row.rejection_reason,
                reason_text=language.REJECTION_WORDING[row.rejection_reason],
                synthetic=row.synthetic,
            )
            for row in rejected
        ],
        symptom_events=_events(db, SymptomEvent, episode.id),
        red_flag_events=_events(db, RedFlagEvent, episode.id),
        medication_log=_medication_log(db, episode, now),
        calibration_history=_calibration_history(db, episode.patient_id),
        session_yield=SummarySessionYield(
            sessions_submitted=len(sessions),
            sessions_completed=len(completed),
            sessions_rejected=len(rejected),
            estimates_produced=len(estimate_rows),
            completion_rate=(len(completed) / len(sessions)) if sessions else 0.0,
            rejections_by_reason=dict(
                Counter(row.rejection_reason.value for row in rejected)
            ),
        ),
        days_since_last_cuff_reading=(
            (now - cuff_rows[0].taken_at).days if cuff_rows else None
        ),
        active_calibration_age_days=_active_calibration_age_days(db, episode.patient_id, now),
        unsynchronised_sessions=sum(
            1
            for row in sessions
            if (row.received_at - row.started_at).total_seconds()
            > UNSYNCHRONISED_THRESHOLD_HOURS * 3600
        ),
    )


def persist(
    db: Session, *, episode: MonitoringEpisode, summary: ClinicianSummaryOut, viewed: bool
) -> ClinicianSummary:
    """Record what was generated, and whether a clinician saw it.

    Append-only (invariant 5): each generation inserts a row rather than updating the previous
    one, so the record shows what was actually on screen at a given moment rather than only the
    most recent rendering. ``delivered_at`` stays null — notification delivery is out of scope
    (BUILD_SPEC 8).
    """
    row = ClinicianSummary(
        episode_id=episode.id,
        generated_at=summary.generated_at,
        viewed_at=summary.generated_at if viewed else None,
        contents=summary.model_dump(mode="json"),
        synthetic=summary.synthetic,
    )
    db.add(row)
    db.flush()
    return row


def build_list_item(
    db: Session, *, episode: MonitoringEpisode, now: datetime | None = None
) -> EpisodeListItem:
    """One row of the clinician episode list.

    The indicators are all *system* states — session yield, staleness, unsynchronised uploads.
    BUILD_SPEC 5.1 reserves warning treatment for these and forbids it for physiological values.
    """
    now = now or datetime.now(tz=timezone.utc)

    sessions = (
        db.execute(
            select(MeasurementSession).where(MeasurementSession.episode_id == episode.id)
        )
        .scalars()
        .all()
    )
    completed = [s for s in sessions if s.status is SessionStatus.COMPLETED]

    last_cuff = db.execute(
        select(CuffReading.taken_at)
        .where(CuffReading.episode_id == episode.id)
        .order_by(CuffReading.taken_at.desc())
        .limit(1)
    ).scalar_one_or_none()

    open_requests = db.execute(
        select(TrendEstimate.id)
        .join(MeasurementSession, MeasurementSession.id == TrendEstimate.session_id)
        .where(
            MeasurementSession.episode_id == episode.id,
            TrendEstimate.deviation_state == DeviationState.PERSISTENT,
        )
    ).all()

    red_flags = db.execute(
        select(RedFlagEvent.id).where(RedFlagEvent.episode_id == episode.id)
    ).all()

    active_age = _active_calibration_age_days(db, episode.patient_id, now)

    return EpisodeListItem(
        episode_id=episode.id,
        patient_pseudonym=episode.patient.pseudonym,
        clinic_id=episode.patient.clinic_id,
        started_at=episode.started_at,
        ended_at=episode.ended_at,
        synthetic=episode.synthetic,
        sessions_submitted=len(sessions),
        completion_rate=(len(completed) / len(sessions)) if sessions else 0.0,
        days_since_last_cuff_reading=(now - last_cuff).days if last_cuff else None,
        active_calibration_age_days=active_age,
        has_active_calibration=active_age is not None,
        open_cuff_requests=len(open_requests),
        red_flag_count=len(red_flags),
    )


def _events(db: Session, model, episode_id: uuid.UUID) -> list[SummaryEvent]:
    rows = (
        db.execute(
            select(model).where(model.episode_id == episode_id).order_by(model.occurred_at.desc())
        )
        .scalars()
        .all()
    )
    return [
        SummaryEvent(occurred_at=row.occurred_at, payload=row.payload, synthetic=row.synthetic)
        for row in rows
    ]


def _medication_log(
    db: Session, episode: MonitoringEpisode, now: datetime
) -> SummaryMedicationLog:
    """Count what was logged. Invariant 6 — a count, not an adherence verdict."""
    rows = (
        db.execute(
            select(MedicationEvent)
            .where(MedicationEvent.episode_id == episode.id)
            .order_by(MedicationEvent.occurred_at)
        )
        .scalars()
        .all()
    )
    distinct_days = {row.occurred_at.date() for row in rows}
    end = episode.ended_at or now
    return SummaryMedicationLog(
        events_logged=len(rows),
        days_with_a_log=len(distinct_days),
        episode_days_elapsed=max(0, (end - episode.started_at).days),
        first_logged_at=rows[0].occurred_at if rows else None,
        last_logged_at=rows[-1].occurred_at if rows else None,
    )


def _calibration_history(
    db: Session, patient_id: uuid.UUID
) -> list[SummaryCalibrationVersion]:
    """Invariant 4 — the full version chain, so supersession is visible."""
    rows = db.execute(
        select(Calibration, DeviceProfile)
        .join(DeviceProfile, DeviceProfile.id == Calibration.device_profile_id)
        .where(Calibration.patient_id == patient_id)
        .order_by(Calibration.established_at.desc())
    ).all()
    return [
        SummaryCalibrationVersion(
            id=calibration.id,
            device_profile_id=calibration.device_profile_id,
            device_model=device.model,
            baseline_mean_ms=calibration.baseline_mean_ms,
            baseline_sd_ms=calibration.baseline_sd_ms,
            n_sessions=calibration.n_sessions,
            status=calibration.status,
            established_at=calibration.established_at,
            superseded_at=calibration.superseded_at,
            superseded_by_id=calibration.superseded_by_id,
            reference_cuff_reading_id=calibration.reference_cuff_reading_id,
            synthetic=calibration.synthetic,
        )
        for calibration, device in rows
    ]


def _active_calibration_age_days(
    db: Session, patient_id: uuid.UUID, now: datetime
) -> int | None:
    """Age of the most recently established active calibration, in days."""
    from app.models import CalibrationStatus

    established = db.execute(
        select(Calibration.established_at)
        .where(
            Calibration.patient_id == patient_id,
            Calibration.status == CalibrationStatus.ACTIVE,
        )
        .order_by(Calibration.established_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    return (now - established).days if established else None
