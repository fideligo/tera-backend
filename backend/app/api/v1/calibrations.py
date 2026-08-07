"""POST /v1/calibrations — establish or supersede a calibration (invariant 4)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import (
    HTTP_422_UNPROCESSABLE,
    DbDep,
    PrincipalDep,
    SettingsDep,
    assert_patient_scope,
)
from app.logging_config import get_logger
from app.models import AuditAction, Calibration
from app.schemas.clinical import CalibrationCreate, CalibrationOut
from app.schemas.common import SyntheticFlag
from app.services import audit
from app.services import calibration as calibration_service
from app.services.calibration import CalibrationError

router = APIRouter(prefix="/calibrations", tags=["calibrations"])
log = get_logger(__name__)


@router.post(
    "",
    response_model=CalibrationOut,
    status_code=status.HTTP_201_CREATED,
    summary="Establish a calibration, superseding any active one for the same device",
)
def create_calibration(
    body: CalibrationCreate,
    db: DbDep,
    principal: PrincipalDep,
    settings: SettingsDep,
) -> CalibrationOut:
    """Compute a baseline from the named sessions and make it the active calibration.

    Recalibration inserts a new row and marks the old one superseded. The old row's baseline is
    never touched — a database trigger enforces that independently of this route (invariant 4).
    """
    assert_patient_scope(principal, body.patient_id)

    try:
        established = calibration_service.establish(
            db,
            patient_id=body.patient_id,
            device_profile_id=body.device_profile_id,
            reference_cuff_reading_id=body.reference_cuff_reading_id,
            session_ids=body.session_ids,
            settings=settings,
            synthetic=body.synthetic,
        )
    except CalibrationError as exc:
        db.rollback()
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE,
            detail={
                "detail": "calibration could not be established",
                "violations": [{"field": exc.field, "message": exc.message}],
            },
        ) from exc

    audit.record(
        db,
        principal=principal,
        action=AuditAction.CALIBRATION_ESTABLISHED,
        target=established.calibration.id,
    )
    if established.superseded is not None:
        audit.record(
            db,
            principal=principal,
            action=AuditAction.CALIBRATION_SUPERSEDED,
            target=established.superseded.id,
        )
    db.commit()

    # Baseline values are clinical content and are excluded from the log line by the redacting
    # formatter; the ids and counts are what an operator needs.
    log.info(
        "calibration_established",
        extra={
            "calibration_id": str(established.calibration.id),
            "device_profile_id": str(established.calibration.device_profile_id),
            "n_sessions": established.calibration.n_sessions,
            "superseded": established.superseded is not None,
        },
    )

    return _render(established.calibration, list(established.source_session_ptts))


@router.get(
    "/{calibration_id}",
    response_model=CalibrationOut,
    summary="Fetch a calibration, including its supersession state",
)
def get_calibration(
    calibration_id: uuid.UUID, db: DbDep, principal: PrincipalDep
) -> CalibrationOut:
    calibration = db.get(Calibration, calibration_id)
    if calibration is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="calibration not found"
        )
    if not principal.is_clinician:
        assert_patient_scope(principal, calibration.patient_id)

    return _render(
        calibration, [link.session_id for link in calibration.source_sessions]
    )


def _render(calibration: Calibration, source_session_ids: list[uuid.UUID]) -> CalibrationOut:
    return CalibrationOut(
        id=calibration.id,
        patient_id=calibration.patient_id,
        device_profile_id=calibration.device_profile_id,
        reference_cuff_reading_id=calibration.reference_cuff_reading_id,
        baseline_mean_ms=calibration.baseline_mean_ms,
        baseline_sd_ms=calibration.baseline_sd_ms,
        n_sessions=calibration.n_sessions,
        status=calibration.status,
        superseded_by_id=calibration.superseded_by_id,
        established_at=calibration.established_at,
        superseded_at=calibration.superseded_at,
        synthetic=calibration.synthetic,
        synthetic_notice=SyntheticFlag.notice_for(calibration.synthetic),
        source_session_ids=source_session_ids,
    )
