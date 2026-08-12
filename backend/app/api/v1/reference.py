"""The BP reference (PM spec sections 12, 27 and 30's ``/bp-reference``).

Three routes and one rule:

* ``POST /v1/bp-reference`` names a confirmed cuff reading as the active baseline, superseding
  whichever one was active before.
* ``GET /v1/bp-reference/current`` returns the active one.
* ``GET /v1/bp-reference/status`` answers "does this patient need to take a cuff reading before
  their next check", which is section 11's routing question and the only one the flow branches on.

# Why the server answers a question the handset already answers

`AppFlowState.reference` computes a local version so the check flow works offline, and that stays.
But the local copy cannot survive a reinstall, cannot see a medication change recorded from
another device, and cannot know when the last *accepted* sensor session ran. Those are the three
inputs to section 27's rule. So the handset's answer is the fallback and this is the authority.

# What it does not do

It never fabricates a reason. `needs_refresh` is true only when one of section 28's named refresh
reasons actually applies, and the reason is returned rather than described — the wording belongs
to the client, the fact belongs here.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import desc, select

from app.api.deps import DbDep, PrincipalDep, SettingsDep
from app.logging_config import get_logger
from app.models import (
    AuditAction,
    BpReference,
    BpReferenceRefreshReason,
    BpReferenceStatus,
    CuffReading,
    MeasurementSession,
    MonitoringEpisode,
    Patient,
    SessionStatus,
)
from app.schemas.reference import (
    BpReferenceCreate,
    BpReferenceOut,
    BpReferenceStatusOut,
    ReferenceReadingOut,
)
from app.services import audit

router = APIRouter(prefix="/bp-reference", tags=["bp-reference"])
log = get_logger(__name__)


def _require_patient(principal) -> uuid.UUID:
    if principal.patient_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only a patient account has a blood-pressure reference",
        )
    return principal.patient_id


def _reading_out(reading: CuffReading) -> ReferenceReadingOut:
    return ReferenceReadingOut(
        systolic=reading.systolic_mmhg,
        diastolic=reading.diastolic_mmhg,
        pulse=reading.pulse_bpm,
        measured_at=reading.taken_at,
    )


def _reference_out(db, row: BpReference) -> BpReferenceOut:
    reading = db.get(CuffReading, row.cuff_reading_id)
    return BpReferenceOut(
        id=row.id,
        patient_id=row.patient_id,
        cuff_reading_id=row.cuff_reading_id,
        activated_at=row.activated_at,
        deactivated_at=row.deactivated_at,
        refresh_reason=row.refresh_reason,
        status=row.status,
        reading=_reading_out(reading),
        synthetic=row.synthetic,
    )


def _active_reference(db, patient_id: uuid.UUID) -> BpReference | None:
    return db.execute(
        select(BpReference)
        .where(
            BpReference.patient_id == patient_id,
            BpReference.status == BpReferenceStatus.ACTIVE,
        )
        .order_by(desc(BpReference.activated_at))
    ).scalars().first()


@router.post(
    "",
    response_model=BpReferenceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Make a confirmed cuff reading the active reference",
)
def set_reference(
    body: BpReferenceCreate, db: DbDep, principal: PrincipalDep
) -> BpReferenceOut:
    """Activate a reference, superseding the current one.

    The reading must already exist, must belong to this patient, and must be a real cuff
    measurement — all three are checked here rather than trusted, because this is the value every
    later trend is read against.
    """
    patient_id = _require_patient(principal)

    reading = db.get(CuffReading, body.cuff_reading_id)
    if reading is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="cuff reading not found"
        )

    # Cross-tenant reads are 404 rather than 403 throughout this API: a 403 confirms the row
    # exists, which is itself a disclosure.
    episode = db.get(MonitoringEpisode, reading.episode_id)
    if episode is None or episode.patient_id != patient_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="cuff reading not found"
        )

    now = datetime.now(tz=timezone.utc)
    current = _active_reference(db, patient_id)

    if current is not None:
        if current.cuff_reading_id == body.cuff_reading_id:
            # Already the reference. Returning it rather than superseding it with itself keeps
            # the operation idempotent — a retried request after a dropped response must not
            # write a second row.
            return _reference_out(db, current)

        # Supersede first, and flush, so the partial unique index never sees two active rows.
        current.status = BpReferenceStatus.SUPERSEDED
        current.deactivated_at = now
        db.flush()

    row = BpReference(
        patient_id=patient_id,
        cuff_reading_id=body.cuff_reading_id,
        activated_at=now,
        deactivated_at=None,
        refresh_reason=body.refresh_reason,
        status=BpReferenceStatus.ACTIVE,
        # Invariant 9: a reference built from a seeded reading is itself seeded.
        synthetic=reading.synthetic,
    )
    db.add(row)
    db.flush()

    # PROF-04's flag has done its job once a fresh reference exists.
    episode.force_reference_refresh = False

    audit.record(
        db, principal=principal, action=AuditAction.BP_REFERENCE_ACTIVATED, target=row.id
    )
    db.commit()

    # Ids and the reason only. Never the mmHg — the logging deny-list covers the obvious field
    # names, and there is no reason to hand it a pressure value to catch.
    log.info(
        "bp_reference_activated",
        extra={"reference_id": str(row.id), "refresh_reason": row.refresh_reason.value},
    )

    return _reference_out(db, row)


@router.get(
    "/current", response_model=BpReferenceOut, summary="The active reference, if there is one"
)
def read_current(db: DbDep, principal: PrincipalDep) -> BpReferenceOut:
    patient_id = _require_patient(principal)
    row = _active_reference(db, patient_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no blood-pressure reference has been set yet",
        )
    return _reference_out(db, row)


@router.get(
    "/status",
    response_model=BpReferenceStatusOut,
    summary="Whether a cuff reading is needed before the next check",
)
def read_status(
    db: DbDep, principal: PrincipalDep, settings: SettingsDep
) -> BpReferenceStatusOut:
    """Section 11's routing question, answered from stored facts.

    Bias toward asking (invariant 7). Every ambiguous case — no reference, no reading behind it,
    an unreadable date — resolves to `needs_refresh = true`. The cost of asking is one cuff
    measurement; the cost of not asking is a trend read against a baseline that no longer
    describes the patient.
    """
    patient_id = _require_patient(principal)
    thresholds = settings.reference

    now = datetime.now(tz=timezone.utc)
    current = _active_reference(db, patient_id)

    # The last *completed* sensor session, which is what section 27's gap is measured on. A
    # rejected capture is not a check: invariant 3 keeps the row, but it produced nothing to read
    # a trend from, so it does not close a monitoring gap.
    last_sensor_check_at = db.execute(
        select(MeasurementSession.started_at)
        .join(MonitoringEpisode, MeasurementSession.episode_id == MonitoringEpisode.id)
        .where(
            MonitoringEpisode.patient_id == patient_id,
            MeasurementSession.status == SessionStatus.COMPLETED,
        )
        .order_by(desc(MeasurementSession.started_at))
        .limit(1)
    ).scalar_one_or_none()

    if current is None:
        return BpReferenceStatusOut(
            has_reference=False,
            needs_refresh=True,
            reason=BpReferenceRefreshReason.FIRST_REFERENCE,
            last_sensor_check_at=last_sensor_check_at,
            current_reference=None,
            reference_age_days=None,
        )

    reading = db.get(CuffReading, current.cuff_reading_id)
    if reading is None:
        # Structurally impossible — the FK is RESTRICT — but a reference whose reading cannot be
        # read is exactly the ambiguity invariant 7 says to resolve by asking.
        return BpReferenceStatusOut(
            has_reference=False,
            needs_refresh=True,
            reason=BpReferenceRefreshReason.FIRST_REFERENCE,
            last_sensor_check_at=last_sensor_check_at,
            current_reference=None,
            reference_age_days=None,
        )

    age_days = max(0, (now - current.activated_at).days)

    # Ordered by which fact is the strongest reason to re-measure, so a patient who has both a
    # medication change and an old reference is told about the medication change.
    reason: BpReferenceRefreshReason | None = None

    forced = db.execute(
        select(MonitoringEpisode.force_reference_refresh)
        .where(MonitoringEpisode.patient_id == patient_id)
        .order_by(desc(MonitoringEpisode.started_at))
        .limit(1)
    ).scalar_one_or_none()

    if forced:
        reason = BpReferenceRefreshReason.MEDICATION_CHANGE
    elif last_sensor_check_at is not None and (
        now - last_sensor_check_at
    ) > timedelta(days=thresholds.monitoring_gap_days):
        reason = BpReferenceRefreshReason.MONITORING_GAP
    elif age_days > thresholds.reference_validity_days:
        reason = BpReferenceRefreshReason.MANUAL_REFRESH

    return BpReferenceStatusOut(
        has_reference=True,
        needs_refresh=reason is not None,
        reason=reason,
        last_sensor_check_at=last_sensor_check_at,
        current_reference=_reading_out(reading),
        reference_age_days=age_days,
    )
