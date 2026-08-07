"""Assemble the patient timeline.

Every record is rendered as its own type (see ``app/schemas/timeline.py``). Nothing here
flattens the three kinds into a common shape — that flattening is exactly how an estimate ends
up rendered as a measurement.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    CuffReading,
    MeasurementSession,
    MedicationEvent,
    MonitoringEpisode,
    RedFlagEvent,
    SessionStatus,
    SymptomEvent,
    TrendEstimate,
)
from app.schemas.common import SyntheticFlag
from app.schemas.timeline import (
    TimelineCuffReading,
    TimelineEvent,
    TimelineOut,
    TimelineRejectedSession,
    TimelineTrendEstimate,
)
from app.services import language


def build(db: Session, *, episode: MonitoringEpisode) -> TimelineOut:
    """Build the whole timeline for one episode, newest first."""
    items: list = []
    items.extend(_cuff_readings(db, episode.id))
    items.extend(_estimates(db, episode.id))
    items.extend(_rejected_sessions(db, episode.id))
    items.extend(_events(db, episode.id))

    items.sort(key=lambda item: item.occurred_at, reverse=True)

    contains_synthetic = episode.synthetic or any(item.synthetic for item in items)
    return TimelineOut(
        episode_id=episode.id,
        patient_pseudonym=episode.patient.pseudonym,
        started_at=episode.started_at,
        ended_at=episode.ended_at,
        contains_synthetic_data=contains_synthetic,
        synthetic_notice=SyntheticFlag.notice_for(contains_synthetic),
        items=items,
    )


def _cuff_readings(db: Session, episode_id: uuid.UUID) -> list[TimelineCuffReading]:
    rows = (
        db.execute(select(CuffReading).where(CuffReading.episode_id == episode_id))
        .scalars()
        .all()
    )
    return [
        TimelineCuffReading(
            id=row.id,
            occurred_at=row.taken_at,
            systolic_mmhg=row.systolic_mmhg,
            diastolic_mmhg=row.diastolic_mmhg,
            pulse_bpm=row.pulse_bpm,
            source=row.source,
            user_confirmed_at=row.user_confirmed_at,
            corrects_id=row.corrects_id,
            synthetic=row.synthetic,
            synthetic_notice=SyntheticFlag.notice_for(row.synthetic),
        )
        for row in rows
    ]


def _estimates(db: Session, episode_id: uuid.UUID) -> list[TimelineTrendEstimate]:
    rows = db.execute(
        select(TrendEstimate, MeasurementSession)
        .join(MeasurementSession, MeasurementSession.id == TrendEstimate.session_id)
        .where(MeasurementSession.episode_id == episode_id)
    ).all()
    return [
        TimelineTrendEstimate(
            id=estimate.id,
            occurred_at=session_row.started_at,
            session_id=session_row.id,
            calibration_id=estimate.calibration_id,
            direction=estimate.direction,
            magnitude_sd=estimate.magnitude_sd,
            confidence=estimate.confidence,
            deviation_state=estimate.deviation_state,
            interpretation=language.DIRECTION_WORDING[estimate.direction],
            synthetic=estimate.synthetic,
            synthetic_notice=SyntheticFlag.notice_for(estimate.synthetic),
        )
        for estimate, session_row in rows
    ]


def _rejected_sessions(db: Session, episode_id: uuid.UUID) -> list[TimelineRejectedSession]:
    """Invariant 3 — rejected sessions are part of the timeline, not filtered out of it."""
    rows = (
        db.execute(
            select(MeasurementSession).where(
                MeasurementSession.episode_id == episode_id,
                MeasurementSession.status == SessionStatus.REJECTED,
            )
        )
        .scalars()
        .all()
    )
    return [
        TimelineRejectedSession(
            id=row.id,
            occurred_at=row.started_at,
            session_id=row.id,
            rejection_reason=row.rejection_reason,
            reason_text=language.REJECTION_WORDING[row.rejection_reason],
            posture=row.posture,
            n_beats_total=row.n_beats_total,
            n_beats_usable=row.n_beats_usable,
            synthetic=row.synthetic,
            synthetic_notice=SyntheticFlag.notice_for(row.synthetic),
        )
        for row in rows
    ]


def _events(db: Session, episode_id: uuid.UUID) -> list[TimelineEvent]:
    from app.models.enums import EventType

    sources = (
        (MedicationEvent, "medication_event", EventType.MEDICATION),
        (SymptomEvent, "symptom_event", EventType.SYMPTOM),
        (RedFlagEvent, "red_flag_event", EventType.RED_FLAG),
    )

    items: list[TimelineEvent] = []
    for model, record_type, event_type in sources:
        rows = (
            db.execute(select(model).where(model.episode_id == episode_id)).scalars().all()
        )
        items.extend(
            TimelineEvent(
                record_type=record_type,
                id=row.id,
                occurred_at=row.occurred_at,
                event_type=event_type,
                payload=row.payload,
                synthetic=row.synthetic,
                synthetic_notice=SyntheticFlag.notice_for(row.synthetic),
            )
            for row in rows
        )
    return items
