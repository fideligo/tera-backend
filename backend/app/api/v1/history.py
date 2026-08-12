"""Section 26's History, over the whole patient rather than one episode (PM spec section 30).

``GET /v1/episodes/{id}/timeline`` already assembles cuff readings, trend estimates, rejected
sessions and events for one episode. History is the same material asked for the way the app asks:
by patient, over a time range, with a type filter, newest first.

# Why a separate route rather than a parameter on the timeline

The timeline is the clinician-facing view of one monitoring episode and its shape is set by
BUILD_SPEC 4.5. History is patient-facing and spans episodes — a self-registered patient has one
today, but nothing guarantees that, and a screen that silently showed one episode's worth of a
patient's own history would be wrong in a way nobody would notice.

# Rejected sessions appear here

Invariant 3, and section 26.3 asks for them explicitly. A check that did not produce a usable
signal is part of the record: the patient did the measurement, and a history that quietly omits
the attempts reads as a cleaner record than the one that exists.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import desc, select

from app.api.deps import DbDep, PrincipalDep, require_patient
from app.logging_config import get_logger
from app.models import (
    CheckSession,
    CuffReading,
    MeasurementSession,
    MonitoringEpisode,
    SessionStatus,
    TrendEstimate,
)
from app.schemas.history import HistoryEntryOut, HistoryOut
from app.services import language

router = APIRouter(prefix="/history", tags=["history"])
log = get_logger(__name__)

#: ``?range=`` values and the window each names. ``all`` is unbounded.
_RANGES: dict[str, timedelta | None] = {
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
    "all": None,
}

HistoryType = Literal["all", "cuff_reading", "trend", "rejected", "check"]


def _episode_ids(db, patient_id: uuid.UUID) -> list[uuid.UUID]:
    return list(
        db.execute(
            select(MonitoringEpisode.id).where(MonitoringEpisode.patient_id == patient_id)
        )
        .scalars()
        .all()
    )


@router.get("", response_model=HistoryOut, summary="HIST-01 — the patient's own record")
def read_history(
    db: DbDep,
    principal: PrincipalDep,
    range: str = Query(default="30d", description="7d, 30d, 90d or all."),
    type: HistoryType = Query(default="all"),
    limit: int = Query(default=100, ge=1, le=500),
) -> HistoryOut:
    """Everything recorded for this patient in the window, newest first.

    One flat list of typed entries rather than four parallel arrays: HIST-01 renders a single
    reverse-chronological column, and interleaving four lists client-side is how two clients end
    up ordering the same history differently.
    """
    patient_id = require_patient(principal)

    if range not in _RANGES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"range must be one of: {', '.join(_RANGES)}",
        )

    window = _RANGES[range]
    since = None if window is None else datetime.now(tz=timezone.utc) - window
    episode_ids = _episode_ids(db, patient_id)
    if not episode_ids:
        return HistoryOut(range=range, type=type, entries=[])

    entries: list[HistoryEntryOut] = []

    if type in {"all", "cuff_reading"}:
        query = select(CuffReading).where(CuffReading.episode_id.in_(episode_ids))
        if since is not None:
            query = query.where(CuffReading.taken_at >= since)
        for row in db.execute(query.order_by(desc(CuffReading.taken_at)).limit(limit)).scalars():
            entries.append(
                HistoryEntryOut(
                    id=row.id,
                    entry_type="cuff_reading",
                    occurred_at=row.taken_at,
                    # The one place in History that carries mmHg, and it is a cuff reading —
                    # invariant 1. The badge travels with it so a client cannot render it in the
                    # same visual language as an estimate.
                    systolic_mmhg=row.systolic_mmhg,
                    diastolic_mmhg=row.diastolic_mmhg,
                    pulse_bpm=row.pulse_bpm,
                    badge=language.CUFF_BADGE,
                    synthetic=row.synthetic,
                )
            )

    if type in {"all", "trend"}:
        query = (
            select(TrendEstimate, MeasurementSession)
            .join(MeasurementSession, TrendEstimate.session_id == MeasurementSession.id)
            .where(MeasurementSession.episode_id.in_(episode_ids))
        )
        if since is not None:
            query = query.where(MeasurementSession.started_at >= since)
        for estimate, session in db.execute(
            query.order_by(desc(MeasurementSession.started_at)).limit(limit)
        ):
            entries.append(
                HistoryEntryOut(
                    id=estimate.id,
                    entry_type="trend",
                    occurred_at=session.started_at,
                    # Direction and magnitude in baseline SDs. **No mmHg** — invariant 1, and the
                    # schema has no field that could carry one.
                    direction=estimate.direction.value,
                    magnitude_sd=estimate.magnitude_sd,
                    deviation_state=(
                        estimate.deviation_state.value
                        if estimate.deviation_state is not None
                        else None
                    ),
                    synthetic=estimate.synthetic,
                )
            )

    if type in {"all", "rejected"}:
        query = select(MeasurementSession).where(
            MeasurementSession.episode_id.in_(episode_ids),
            MeasurementSession.status == SessionStatus.REJECTED,
        )
        if since is not None:
            query = query.where(MeasurementSession.started_at >= since)
        for row in db.execute(
            query.order_by(desc(MeasurementSession.started_at)).limit(limit)
        ).scalars():
            entries.append(
                HistoryEntryOut(
                    id=row.id,
                    entry_type="rejected",
                    occurred_at=row.started_at,
                    rejection_reason=(
                        row.rejection_reason.value if row.rejection_reason is not None else None
                    ),
                    synthetic=row.synthetic,
                )
            )

    if type in {"all", "check"}:
        query = select(CheckSession).where(CheckSession.episode_id.in_(episode_ids))
        if since is not None:
            query = query.where(CheckSession.started_at >= since)
        for row in db.execute(
            query.order_by(desc(CheckSession.started_at)).limit(limit)
        ).scalars():
            entries.append(
                HistoryEntryOut(
                    id=row.id,
                    entry_type="check",
                    occurred_at=row.started_at,
                    mode=row.mode.value,
                    check_status=row.status.value,
                    synthetic=row.synthetic,
                )
            )

    entries.sort(key=lambda e: e.occurred_at, reverse=True)
    return HistoryOut(range=range, type=type, entries=entries[:limit])


@router.get(
    "/{event_id}", response_model=HistoryEntryOut, summary="HIST-02 — one history entry"
)
def read_history_entry(
    event_id: uuid.UUID, db: DbDep, principal: PrincipalDep
) -> HistoryEntryOut:
    """One entry, looked up by id across the four kinds.

    The id alone does not say which table it came from, so all four are tried. Cross-tenant ids
    are 404 like everywhere else — a 403 would confirm the row exists.
    """
    patient_id = require_patient(principal)
    episode_ids = _episode_ids(db, patient_id)

    reading = db.get(CuffReading, event_id)
    if reading is not None and reading.episode_id in episode_ids:
        return HistoryEntryOut(
            id=reading.id,
            entry_type="cuff_reading",
            occurred_at=reading.taken_at,
            systolic_mmhg=reading.systolic_mmhg,
            diastolic_mmhg=reading.diastolic_mmhg,
            pulse_bpm=reading.pulse_bpm,
            badge=language.CUFF_BADGE,
            synthetic=reading.synthetic,
        )

    session = db.get(MeasurementSession, event_id)
    if session is not None and session.episode_id in episode_ids:
        estimate = session.estimate
        if session.status is SessionStatus.REJECTED or estimate is None:
            return HistoryEntryOut(
                id=session.id,
                entry_type="rejected",
                occurred_at=session.started_at,
                rejection_reason=(
                    session.rejection_reason.value
                    if session.rejection_reason is not None
                    else None
                ),
                synthetic=session.synthetic,
            )
        return HistoryEntryOut(
            id=estimate.id,
            entry_type="trend",
            occurred_at=session.started_at,
            direction=estimate.direction.value,
            magnitude_sd=estimate.magnitude_sd,
            deviation_state=(
                estimate.deviation_state.value
                if estimate.deviation_state is not None
                else None
            ),
            synthetic=estimate.synthetic,
        )

    check = db.get(CheckSession, event_id)
    if check is not None and check.episode_id in episode_ids:
        return HistoryEntryOut(
            id=check.id,
            entry_type="check",
            occurred_at=check.started_at,
            mode=check.mode.value,
            check_status=check.status.value,
            synthetic=check.synthetic,
        )

    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="history entry not found")
