"""PHR profile, per-session context, and the insight (PM spec sections 24, 28, 30).

Three routes, three different storage disciplines, and the differences are the point:

* ``POST /v1/profile`` **updates in place.** A profile describes a person now.
* ``POST /v1/check-sessions/{id}/context`` **inserts.** It describes one moment, and
  ``session_context`` is append-only (invariant 5), so a correction supersedes.
* ``GET /v1/check-sessions/{id}/insight`` **computes and stores nothing.** It is a pure read over
  rows that already exist, so it cannot drift from them.

# Why POST and not PATCH

The PM spec writes both as ``PATCH``, and section 30 opens with "Route names are examples."
Invariant 5 is enforced by a test that walks the OpenAPI schema and refuses **any** PUT, PATCH or
DELETE anywhere in the API — deliberately blunt, because the verb is what a client sees and a
mutable-looking route on a clinical resource is an invitation. The verb is not load-bearing in the
spec; the invariant is load-bearing here. So both are POST, and for the context route POST is the
more honest verb anyway: it inserts.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import desc, select

from app.api.deps import DbDep, PrincipalDep, load_episode
from app.logging_config import get_logger
from app.models import (
    AuditAction,
    Calibration,
    CuffReading,
    MeasurementSession,
    PhrProfile,
    SessionContext,
    SessionStatus,
)
from app.schemas.phr import (
    PhrProfileOut,
    PhrProfilePatch,
    SessionContextOut,
    SessionContextPatch,
)
from app.services import audit, contraindication, language
from app.services.insight import Insight, InsightFeatures, evaluate

router = APIRouter(tags=["phr"])
log = get_logger(__name__)


def _require_patient(principal) -> uuid.UUID:
    if principal.patient_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="only a patient account has a health profile",
        )
    return principal.patient_id


def _profile_out(row: PhrProfile) -> PhrProfileOut:
    return PhrProfileOut(
        patient_id=row.patient_id,
        date_of_birth=row.date_of_birth,
        sex_assigned_at_birth=row.sex_assigned_at_birth,
        height_cm=row.height_cm,
        weight_kg=row.weight_kg,
        hypertension_status=row.hypertension_status,
        taking_bp_medication=row.taking_bp_medication,
        conditions=list(row.conditions),
        updated_at=row.updated_at,
        synthetic=row.synthetic,
    )


@router.get("/profile", response_model=PhrProfileOut, summary="The patient's health profile")
def read_profile(db: DbDep, principal: PrincipalDep) -> PhrProfileOut:
    patient_id = _require_patient(principal)
    row = db.execute(
        select(PhrProfile).where(PhrProfile.patient_id == patient_id)
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no profile has been recorded yet"
        )
    return _profile_out(row)


@router.post("/profile", response_model=PhrProfileOut, summary="Update the health profile")
def patch_profile(
    body: PhrProfilePatch, db: DbDep, principal: PrincipalDep
) -> PhrProfileOut:
    """Create or update, field by field.

    Only fields present in the request are touched. An absent field means "unchanged", not
    "clear" — otherwise a screen that collects half the profile would erase the other half every
    time it saved.
    """
    patient_id = _require_patient(principal)

    row = db.execute(
        select(PhrProfile).where(PhrProfile.patient_id == patient_id)
    ).scalar_one_or_none()
    if row is None:
        row = PhrProfile(patient_id=patient_id, conditions=[], synthetic=False)
        db.add(row)

    supplied = body.model_dump(exclude_unset=True)
    for field_name, value in supplied.items():
        setattr(row, field_name, value)
    row.updated_at = datetime.now(tz=timezone.utc)

    db.flush()
    audit.record(db, principal=principal, action=AuditAction.PHR_PROFILE_UPDATED, target=row.id)
    db.commit()

    # Field names only. Never a date of birth, a weight or a condition — all clinical content, and
    # the deny-list covers the ones with obvious names.
    log.info("phr_profile_updated", extra={"fields": sorted(supplied.keys())})

    return _profile_out(row)


def _load_session(session_id: uuid.UUID, principal, db):
    """The session and its episode. Authorisation runs through `load_episode`, the same path
    every other clinical read uses, and the episode is returned rather than discarded because the
    contraindication gate needs the patient id."""
    stored = db.get(MeasurementSession, session_id)
    if stored is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="session not found")
    episode = load_episode(stored.episode_id, principal, db)
    return stored, episode


def _context_out(row: SessionContext) -> SessionContextOut:
    return SessionContextOut(
        id=row.id,
        session_id=row.session_id,
        recorded_at=row.recorded_at,
        sleep_less_than_usual=row.sleep_less_than_usual,
        stress_higher_than_usual=row.stress_higher_than_usual,
        feeling_unwell=row.feeling_unwell,
        symptoms=list(row.symptoms),
        medication_status_today=row.medication_status_today,
        synthetic=row.synthetic,
    )


def _latest_context(db, session_id: uuid.UUID) -> SessionContext | None:
    return db.execute(
        select(SessionContext)
        .where(SessionContext.session_id == session_id)
        .order_by(desc(SessionContext.recorded_at), desc(SessionContext.id))
        .limit(1)
    ).scalar_one_or_none()


@router.post(
    "/check-sessions/{session_id}/context",
    response_model=SessionContextOut,
    status_code=status.HTTP_201_CREATED,
    summary="Record CTX-01 for a check",
)
def patch_session_context(
    session_id: uuid.UUID, body: SessionContextPatch, db: DbDep, principal: PrincipalDep
) -> SessionContextOut:
    """A patch by name, an insert by storage.

    `session_context` is append-only, so a correction adds a row and reads take the latest. What
    the patient reported around a past measurement is a fact about that moment, and rewriting it
    is what invariant 5 exists to prevent.
    """
    stored, _episode = _load_session(session_id, principal, db)

    row = SessionContext(
        session_id=stored.id,
        recorded_at=datetime.now(tz=timezone.utc),
        sleep_less_than_usual=body.sleep_less_than_usual,
        stress_higher_than_usual=body.stress_higher_than_usual,
        feeling_unwell=body.feeling_unwell,
        symptoms=list(body.symptoms),
        medication_status_today=body.medication_status_today,
        synthetic=False,
    )
    db.add(row)
    db.flush()

    audit.record(
        db, principal=principal, action=AuditAction.SESSION_CONTEXT_RECORDED, target=row.id
    )
    db.commit()

    # Counts and ids. Never the symptom list or the medication answer.
    log.info(
        "session_context_recorded",
        extra={"session_id": str(stored.id), "symptom_count": len(row.symptoms)},
    )

    return _context_out(row)


@router.get(
    "/check-sessions/{session_id}/context",
    response_model=SessionContextOut,
    summary="The context in force for a check",
)
def read_session_context(
    session_id: uuid.UUID, db: DbDep, principal: PrincipalDep
) -> SessionContextOut:
    stored, _episode = _load_session(session_id, principal, db)
    row = _latest_context(db, stored.id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no context has been recorded for this check",
        )
    return _context_out(row)


def _build_features(db, stored: MeasurementSession, context: SessionContext | None) -> InsightFeatures:
    """Assemble what the matrix reads. All IO happens here; `evaluate` does none."""
    estimate = stored.estimate

    # The reference is the cuff reading the active calibration was anchored to. Invariant 1: this
    # is the only place a pressure value can come from, and it is never derived from the trend.
    reference: CuffReading | None = None
    if stored.calibration_id is not None:
        calibration = db.get(Calibration, stored.calibration_id)
        if calibration is not None and calibration.reference_cuff_reading_id is not None:
            reference = db.get(CuffReading, calibration.reference_cuff_reading_id)

    # A cuff reading taken as part of this check supersedes the reference for wording.
    confirmed = db.execute(
        select(CuffReading)
        .where(
            CuffReading.episode_id == stored.episode_id,
            CuffReading.taken_at >= stored.started_at,
        )
        .order_by(desc(CuffReading.taken_at))
        .limit(1)
    ).scalar_one_or_none()

    quality = stored.quality or {}
    # "HR near resting" is not measured directly; motion is the proxy the capture reports, and it
    # is the same signal the comparability rows in section 24 are really about.
    motion = quality.get("motion_index")
    hr_near_resting = motion is None or float(motion) < 0.5

    return InsightFeatures(
        sensor_mode=estimate is not None or stored.status is SessionStatus.COMPLETED,
        trend_direction=estimate.direction if estimate is not None else None,
        deviation_state=estimate.deviation_state if estimate is not None else None,
        reference_systolic=reference.systolic_mmhg if reference is not None else None,
        reference_diastolic=reference.diastolic_mmhg if reference is not None else None,
        confirmed_systolic=confirmed.systolic_mmhg if confirmed is not None else None,
        confirmed_diastolic=confirmed.diastolic_mmhg if confirmed is not None else None,
        hr_near_resting=hr_near_resting,
        precondition_standard=True,
        medication_status=(
            context.medication_status_today if context is not None else None
        ),
        sleep_less_than_usual=context.sleep_less_than_usual if context is not None else False,
        stress_higher_than_usual=(
            context.stress_higher_than_usual if context is not None else False
        ),
        session_rejected=stored.status is SessionStatus.REJECTED,
    )


def _render(insight: Insight) -> dict:
    """Apply the language layer. Decision and wording stay separable."""
    return {
        **insight.to_json(),
        "hero": language.RESULT_STATE_WORDING[insight.result_state.value],
        "next_best_step": language.PRIORITY_ACTION_WORDING[insight.priority_action_code.value],
        "context_chips": [
            language.CONTEXT_CODE_WORDING[code]
            for code in insight.context_codes
            if code in language.CONTEXT_CODE_WORDING
        ],
        "context_disclaimer": language.CONTEXT_DISCLAIMER,
        "notice": language.INSIGHT_NOTICE,
    }


@router.get(
    "/check-sessions/{session_id}/insight",
    summary="The deterministic insight for a check",
)
def read_insight(session_id: uuid.UUID, db: DbDep, principal: PrincipalDep) -> dict:
    """Computed on read, stored nowhere.

    An insight is a function of rows that already exist, so recomputing it cannot drift from them
    — and there is no second copy to keep in step. The rule engine is pure; every read of the same
    session returns the same verdict.
    """
    stored, episode = _load_session(session_id, principal, db)

    # The contraindication gate applies here too: an estimate withheld on the session detail must
    # not reappear wrapped in an insight.
    if contraindication.is_contraindicated(db, episode.patient_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=language.CONTRAINDICATED_PREGNANCY
        )

    context = _latest_context(db, stored.id)
    insight = evaluate(_build_features(db, stored, context))

    return {
        "session_id": str(stored.id),
        "synthetic": stored.synthetic,
        **_render(insight),
        "around_this_check": None if context is None else _context_out(context).as_features(),
    }
