"""Patient-supplied clinical context.

B2C PIVOT. The intake form on the handset is the only source of medication, pregnancy and rhythm
history once there is no clinic behind the account. It was handset-only, which meant it vanished on
uninstall and the server could not see a contraindication it was expected to respect.

**Append-only, latest-wins** (invariant 5). Every submission inserts a row. A changed answer does
not erase the previous one: what the patient said in June is a fact about June.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import desc, select

from app.api.deps import DbDep, PrincipalDep
from app.logging_config import get_logger
from app.models import AuditAction, PatientContext
from app.schemas.context import PatientContextCreate, PatientContextOut
from app.services import audit

router = APIRouter(prefix="/patient-context", tags=["patient-context"])
log = get_logger(__name__)


def _require_patient(principal) -> None:
    """Context belongs to a patient record, and the token is what says which one.

    A clinician or admin token has no patient_id, so there is nothing to file against. 403 rather
    than 422: the request is well-formed, the caller is simply not the kind of principal this
    route serves.
    """
    if principal.patient_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only a patient account has clinical context",
        )


def _to_out(row: PatientContext) -> PatientContextOut:
    return PatientContextOut(
        id=row.id,
        patient_id=row.patient_id,
        recorded_at=row.recorded_at,
        last_regimen_change_date=row.last_regimen_change_date,
        medications=row.medications,
        pregnant=row.pregnant,
        known_arrhythmia=row.known_arrhythmia,
        last_clinic_systolic_mmhg=row.last_clinic_systolic_mmhg,
        last_clinic_diastolic_mmhg=row.last_clinic_diastolic_mmhg,
        last_clinic_taken_on=row.last_clinic_taken_on,
        synthetic=row.synthetic,
    )


@router.post(
    "",
    response_model=PatientContextOut,
    status_code=status.HTTP_201_CREATED,
    summary="Record the patient's clinical context",
)
def submit_patient_context(
    body: PatientContextCreate, db: DbDep, principal: PrincipalDep
) -> PatientContextOut:
    """File a new context row for the authenticated patient.

    The patient is taken from the token and there is no ``patient_id`` in the body, so context
    cannot be filed against somebody else's record by editing a request.
    """
    _require_patient(principal)

    row = PatientContext(
        patient_id=principal.patient_id,
        recorded_at=datetime.now(tz=timezone.utc),
        last_regimen_change_date=body.last_regimen_change_date,
        medications=[m.model_dump() for m in body.medications],
        pregnant=body.pregnant,
        known_arrhythmia=body.known_arrhythmia,
        last_clinic_systolic_mmhg=body.last_clinic_systolic_mmhg,
        last_clinic_diastolic_mmhg=body.last_clinic_diastolic_mmhg,
        last_clinic_taken_on=body.last_clinic_taken_on,
        synthetic=False,
    )
    db.add(row)
    db.flush()

    audit.record(db, principal=principal, action=AuditAction.PATIENT_CONTEXT_RECORDED, target=row.id)
    db.commit()

    # Ids and counts only. Never the medication names, the pregnancy answer or the pressures —
    # every one of those is on the logging deny-list, and this line does not go near them.
    log.info(
        "patient_context_recorded",
        extra={"context_id": str(row.id), "medication_count": len(row.medications)},
    )

    return _to_out(row)


@router.get(
    "",
    response_model=PatientContextOut,
    summary="The patient's context currently in force",
)
def read_patient_context(db: DbDep, principal: PrincipalDep) -> PatientContextOut:
    """The most recent row. 404 when the patient has never filled the form in."""
    _require_patient(principal)

    row = db.execute(
        select(PatientContext)
        .where(PatientContext.patient_id == principal.patient_id)
        .order_by(desc(PatientContext.recorded_at), desc(PatientContext.id))
        .limit(1)
    ).scalar_one_or_none()

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no clinical context has been recorded for this patient",
        )

    return _to_out(row)
