"""POST /v1/cuff-readings — the only route through which mmHg enters the system."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from app.api.deps import (
    HTTP_422_UNPROCESSABLE,
    DbDep,
    PrincipalDep,
    SettingsDep,
    load_episode,
)
from app.logging_config import get_logger
from app.models import AuditAction, CuffReading, CuffSource
from app.schemas.common import SyntheticFlag
from app.schemas.clinical import CuffReadingCreate, CuffReadingOut
from app.services import audit
from app.services.plausibility import check_cuff_reading

router = APIRouter(prefix="/cuff-readings", tags=["cuff-readings"])
log = get_logger(__name__)


@router.post(
    "",
    response_model=CuffReadingOut,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a user-confirmed cuff reading",
)
def submit_cuff_reading(
    body: CuffReadingCreate,
    db: DbDep,
    principal: PrincipalDep,
    settings: SettingsDep,
) -> CuffReadingOut:
    """Record a validated upper-arm cuff measurement.

    Invariant 1: this is the reference the whole system is built around, and the only place a
    pressure value originates. Invariant 6: nothing is said about what the numbers mean — they
    are recorded, not interpreted.
    """
    episode = load_episode(body.episode_id, principal, db)

    # BUILD_SPEC 8 puts seven-segment OCR out of scope. The enum value exists for schema
    # completeness; accepting it would imply a capability that does not exist (invariant 9).
    if body.source is not CuffSource.MANUAL_ENTRY:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE,
            detail={
                "detail": "unsupported cuff reading source",
                "violations": [
                    {
                        "field": "source",
                        "message": "only 'manual_entry' is accepted. Photograph capture with "
                        "seven-segment OCR is not implemented.",
                    }
                ],
            },
        )
    if body.ocr_confidence is not None:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE,
            detail={
                "detail": "unsupported field",
                "violations": [
                    {
                        "field": "ocr_confidence",
                        "message": "only meaningful for photograph capture, which is not "
                        "implemented.",
                    }
                ],
            },
        )

    violations = check_cuff_reading(
        systolic_mmhg=body.systolic_mmhg,
        diastolic_mmhg=body.diastolic_mmhg,
        pulse_bpm=body.pulse_bpm,
        settings=settings.plausibility,
    )
    if violations:
        raise HTTPException(
            status_code=HTTP_422_UNPROCESSABLE,
            detail={
                "detail": "cuff reading failed validation",
                "violations": [{"field": v.field, "message": v.message} for v in violations],
            },
        )

    if body.corrects_id is not None:
        original = db.get(CuffReading, body.corrects_id)
        if original is None:
            raise HTTPException(
                status_code=HTTP_422_UNPROCESSABLE,
                detail="corrects_id does not name an existing reading",
            )
        if original.episode_id != body.episode_id:
            raise HTTPException(
                status_code=HTTP_422_UNPROCESSABLE,
                detail="a correction must belong to the same episode as the reading it corrects",
            )

    reading = CuffReading(
        episode_id=episode.id,
        systolic_mmhg=body.systolic_mmhg,
        diastolic_mmhg=body.diastolic_mmhg,
        pulse_bpm=body.pulse_bpm,
        source=body.source,
        ocr_confidence=None,
        taken_at=body.taken_at,
        user_confirmed_at=body.user_confirmed_at,
        corrects_id=body.corrects_id,
        synthetic=body.synthetic,
    )
    db.add(reading)
    db.flush()

    audit.record(
        db, principal=principal, action=AuditAction.CUFF_READING_RECORDED, target=reading.id
    )
    db.commit()

    # No pressure values in the log line (BUILD_SPEC 4.5) — the id is enough to find the row.
    log.info(
        "cuff_reading_recorded",
        extra={
            "cuff_reading_id": str(reading.id),
            "episode_id": str(reading.episode_id),
            "is_correction": reading.corrects_id is not None,
        },
    )

    return CuffReadingOut(
        id=reading.id,
        episode_id=reading.episode_id,
        systolic_mmhg=reading.systolic_mmhg,
        diastolic_mmhg=reading.diastolic_mmhg,
        pulse_bpm=reading.pulse_bpm,
        source=reading.source,
        taken_at=reading.taken_at,
        user_confirmed_at=reading.user_confirmed_at,
        corrects_id=reading.corrects_id,
        synthetic=reading.synthetic,
        synthetic_notice=SyntheticFlag.notice_for(reading.synthetic),
    )
