"""Medications, conditions and profile completion (PM spec sections 8, 25 and 30).

The three parts of the PHR that are not single-valued answers on the profile row.

# Nothing here is deleted

Section 30 lists ``DELETE /medications/{id}``. Section 28 gives ``medications`` a ``status``
column, which is the spec answering its own question: a medication somebody stopped taking is not
a row that never existed. What a patient was taking when a reading was recorded is part of reading
that record later, and invariant 5 says the same thing about clinical history generally. So the
delete is a transition to ``stopped``, and the row stays.

# POST, not PATCH or PUT

Same reason as ``phr.py``: a test walks the OpenAPI schema and refuses every PUT, PATCH and DELETE
across the whole API, because the verb is what a client sees and a mutable-looking route on a
clinical resource is an invitation. Section 30 opens with "Route names are examples"; the
invariant is the part that is load-bearing.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import DbDep, PrincipalDep, require_patient
from app.logging_config import get_logger
from app.models import (
    AuditAction,
    Medication,
    MedicationStatus,
    MonitoringEpisode,
    PhrProfile,
)
from app.schemas.phr import (
    ConditionsIn,
    ConditionsOut,
    MedicationIn,
    MedicationOut,
    MedicationUpdate,
    ProfileCompletionOut,
)
from app.services import audit

router = APIRouter(tags=["phr"])
log = get_logger(__name__)


def medication_out(row: Medication) -> MedicationOut:
    """Public: ``phr.py`` embeds the list in the profile response."""
    return MedicationOut(
        id=row.id,
        name=row.name,
        dose=row.dose,
        frequency=row.frequency,
        started_at=row.started_at,
        last_changed_at=row.last_changed_at,
        status=row.status,
        synthetic=row.synthetic,
    )


def active_medications(db, patient_id: uuid.UUID) -> list[Medication]:
    return list(
        db.execute(
            select(Medication)
            .where(
                Medication.patient_id == patient_id,
                Medication.status == MedicationStatus.ACTIVE,
            )
            .order_by(Medication.name)
        )
        .scalars()
        .all()
    )


def _load_medication(medication_id: uuid.UUID, patient_id: uuid.UUID, db) -> Medication:
    row = db.get(Medication, medication_id)
    # 404 rather than 403 on somebody else's row: a 403 confirms it exists.
    if row is None or row.patient_id != patient_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="medication not found")
    return row


def _flag_reference_refresh(db, patient_id: uuid.UUID) -> None:
    """PROF-04: a medication change forces a fresh BP reference.

    The baseline was established under one medication regime, so reading a trend against it after
    that regime changed compares two different states of the same person. Section 27 requires a
    new reference and this is the only place the flag is set.
    """
    episodes = (
        db.execute(select(MonitoringEpisode).where(MonitoringEpisode.patient_id == patient_id))
        .scalars()
        .all()
    )
    for episode in episodes:
        episode.force_reference_refresh = True


@router.get("/medications", response_model=list[MedicationOut], summary="The medication list")
def list_medications(
    db: DbDep, principal: PrincipalDep, include_stopped: bool = False
) -> list[MedicationOut]:
    """Active medications by default; stopped ones on request."""
    patient_id = require_patient(principal)

    query = select(Medication).where(Medication.patient_id == patient_id)
    if not include_stopped:
        query = query.where(Medication.status == MedicationStatus.ACTIVE)

    rows = db.execute(query.order_by(Medication.name)).scalars().all()
    return [medication_out(row) for row in rows]


@router.post(
    "/medications",
    response_model=MedicationOut,
    status_code=status.HTTP_201_CREATED,
    summary="Add a medication",
)
def add_medication(body: MedicationIn, db: DbDep, principal: PrincipalDep) -> MedicationOut:
    patient_id = require_patient(principal)
    now = datetime.now(tz=timezone.utc)

    row = Medication(
        patient_id=patient_id,
        name=body.name.strip(),
        dose=body.dose.strip(),
        frequency=body.frequency.strip(),
        started_at=body.started_at,
        last_changed_at=now.date(),
        status=MedicationStatus.ACTIVE,
        updated_at=now,
        synthetic=False,
    )
    db.add(row)
    db.flush()

    _flag_reference_refresh(db, patient_id)
    audit.record(db, principal=principal, action=AuditAction.MEDICATIONS_UPDATED, target=row.id)
    db.commit()

    # The id only. A drug name is clinical content and says what someone is being treated for.
    log.info("medication_added", extra={"medication_id": str(row.id)})
    return medication_out(row)


@router.post(
    "/medications/{medication_id}", response_model=MedicationOut, summary="Correct a medication"
)
def update_medication(
    medication_id: uuid.UUID, body: MedicationUpdate, db: DbDep, principal: PrincipalDep
) -> MedicationOut:
    """A correction, field by field. An absent field means unchanged, not cleared."""
    patient_id = require_patient(principal)
    row = _load_medication(medication_id, patient_id, db)

    if row.status is MedicationStatus.STOPPED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="this medication has been stopped; add it again to resume it",
        )

    supplied = body.model_dump(exclude_unset=True)
    for field_name, value in supplied.items():
        setattr(row, field_name, value.strip() if isinstance(value, str) else value)

    now = datetime.now(tz=timezone.utc)
    row.last_changed_at = now.date()
    row.updated_at = now
    db.flush()

    _flag_reference_refresh(db, patient_id)
    audit.record(db, principal=principal, action=AuditAction.MEDICATIONS_UPDATED, target=row.id)
    db.commit()

    log.info(
        "medication_updated",
        extra={"medication_id": str(row.id), "fields": sorted(supplied.keys())},
    )
    return medication_out(row)


@router.post(
    "/medications/{medication_id}/stop",
    response_model=MedicationOut,
    summary="Stop taking a medication",
)
def stop_medication(
    medication_id: uuid.UUID, db: DbDep, principal: PrincipalDep
) -> MedicationOut:
    """Section 30's ``DELETE``, as the status transition the data model already provides.

    Idempotent: stopping an already-stopped medication returns it unchanged rather than erroring,
    so a retried request after a dropped response is not a failure.
    """
    patient_id = require_patient(principal)
    row = _load_medication(medication_id, patient_id, db)

    if row.status is MedicationStatus.STOPPED:
        return medication_out(row)

    now = datetime.now(tz=timezone.utc)
    row.status = MedicationStatus.STOPPED
    row.last_changed_at = now.date()
    row.updated_at = now
    db.flush()

    _flag_reference_refresh(db, patient_id)
    audit.record(db, principal=principal, action=AuditAction.MEDICATIONS_UPDATED, target=row.id)
    db.commit()

    log.info("medication_stopped", extra={"medication_id": str(row.id)})
    return medication_out(row)


# =================================================================================== conditions


@router.get("/conditions", response_model=ConditionsOut, summary="Reported conditions")
def read_conditions(db: DbDep, principal: PrincipalDep) -> ConditionsOut:
    patient_id = require_patient(principal)
    row = db.execute(
        select(PhrProfile).where(PhrProfile.patient_id == patient_id)
    ).scalar_one_or_none()
    if row is None:
        # An empty list, not a 404: "nothing reported" is a legitimate answer to PROF-03 and a
        # client should not have to treat a not-yet-created profile as a special case.
        return ConditionsOut(conditions=[], updated_at=datetime.now(tz=timezone.utc))
    return ConditionsOut(conditions=list(row.conditions), updated_at=row.updated_at)


@router.post("/conditions", response_model=ConditionsOut, summary="Replace the condition list")
def replace_conditions(body: ConditionsIn, db: DbDep, principal: PrincipalDep) -> ConditionsOut:
    """The whole list, not a delta.

    PROF-03 is a checklist, and a patient unticking something has to be able to say so — which a
    delta API cannot express without a second verb. Codes are validated against the spec's closed
    list in the schema, so a typo is a 422 rather than a row nobody can query later.
    """
    patient_id = require_patient(principal)

    row = db.execute(
        select(PhrProfile).where(PhrProfile.patient_id == patient_id)
    ).scalar_one_or_none()
    if row is None:
        row = PhrProfile(patient_id=patient_id, conditions=[], synthetic=False)
        db.add(row)

    # Sorted and deduplicated, so the stored value does not depend on tick order and two
    # equivalent requests produce an identical row.
    row.conditions = sorted(set(body.conditions))
    row.updated_at = datetime.now(tz=timezone.utc)
    db.flush()

    audit.record(db, principal=principal, action=AuditAction.PHR_PROFILE_UPDATED, target=row.id)
    db.commit()

    # A count, never the codes: which conditions somebody reported is clinical content.
    log.info("conditions_replaced", extra={"condition_count": len(row.conditions)})
    return ConditionsOut(conditions=list(row.conditions), updated_at=row.updated_at)


# =================================================================================== completion

#: PROF-01's progress meter. A section counts as answered when its required fields are non-null.
#: Height and weight are deliberately excluded: the spec allows skipping them, and a meter that
#: can never reach 100% teaches people to ignore it.
_COMPLETION_SECTIONS: dict[str, tuple[str, ...]] = {
    "about_you": ("date_of_birth", "sex_assigned_at_birth"),
    "health_context": ("hypertension_status", "taking_bp_medication"),
    "lifestyle": ("physical_activity", "smoking_status", "usual_sleep_hours"),
    "family_history": ("family_bp_history",),
}


@router.get(
    "/profile/completion",
    response_model=ProfileCompletionOut,
    summary="How much of the profile has been filled in",
)
def read_completion(db: DbDep, principal: PrincipalDep) -> ProfileCompletionOut:
    """A count of sections answered.

    **Nothing here reads a value.** It reports whether a field is filled, never whether what is in
    it is good or bad — invariant 6, and the spec's own instruction that a BMI must not be
    computed or judged.
    """
    patient_id = require_patient(principal)
    row = db.execute(
        select(PhrProfile).where(PhrProfile.patient_id == patient_id)
    ).scalar_one_or_none()

    completed: list[str] = []
    missing: list[str] = []
    for section, fields in _COMPLETION_SECTIONS.items():
        answered = row is not None and all(getattr(row, f) is not None for f in fields)
        (completed if answered else missing).append(section)

    return ProfileCompletionOut(
        complete=not missing,
        completed_sections=completed,
        missing_sections=missing,
        percent=round(100 * len(completed) / len(_COMPLETION_SECTIONS)),
    )
