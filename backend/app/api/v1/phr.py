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

from app.api.deps import DbDep, PrincipalDep, SettingsDep, load_episode, require_patient
from app.logging_config import get_logger
from app.models import (
    AuditAction,
    Calibration,
    Medication,
    CheckMode,
    CheckSession,
    CheckSessionStatus,
    CuffReading,
    MeasurementSession,
    PhrProfile,
    Precondition,
    SessionContext,
    SessionStatus,
)
from app.api.v1.medications import active_medications, medication_out
from app.schemas.phr import (
    CaptureIn,
    CheckSessionCreate,
    CheckSessionOut,
    CheckSessionStateOut,
    ProcessIn,
    PhrProfileOut,
    PhrProfilePatch,
    PreconditionCreate,
    PreconditionOut,
    SessionContextOut,
    SessionContextPatch,
)
from app.services import (
    audit,
    contraindication,
    language,
    llm_insight,
    pressure_estimate,
)
from app.services.insight import Insight, InsightFeatures, evaluate

router = APIRouter(tags=["phr"])
log = get_logger(__name__)


#: Moved to ``app.api.deps`` so the medications module can share it without importing this one.
_require_patient = require_patient


def _profile_out(row: PhrProfile, medications: list[Medication] | None = None) -> PhrProfileOut:
    """The stored profile, every field of it.

    Built by field name from the model rather than by hand. The hand-written version omitted the
    nine columns migration 0011 added — and because `PhrProfileOut` declares them without
    defaults, every call to `GET /v1/profile` raised a ValidationError and returned 500. A list
    that has to be extended by hand whenever a column lands is a list that will be wrong again.
    """
    return PhrProfileOut(
        patient_id=row.patient_id,
        conditions=list(row.conditions),
        medications=[medication_out(m).model_dump(mode="json") for m in (medications or [])],
        synthetic=row.synthetic,
        **{
            field: getattr(row, field)
            for field in PhrProfileOut.model_fields
            if field not in {"patient_id", "conditions", "medications", "synthetic",
                             "synthetic_notice"}
        },
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
    return _profile_out(row, active_medications(db, patient_id))


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

    return _profile_out(row, active_medications(db, patient_id))


def _load_session(session_id: uuid.UUID, principal, db):
    """A check session and its episode.

    Authorisation runs through `load_episode`, the same path every other clinical read uses, and
    the episode is returned rather than discarded because the contraindication gate needs the
    patient id.
    """
    stored = db.get(CheckSession, session_id)
    if stored is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="check session not found")
    episode = load_episode(stored.episode_id, principal, db)
    return stored, episode


def _check_session_out(row: CheckSession) -> CheckSessionOut:
    return CheckSessionOut(
        id=row.id,
        episode_id=row.episode_id,
        mode=row.mode,
        status=row.status,
        started_at=row.started_at,
        completed_at=row.completed_at,
        synthetic=row.synthetic,
    )


@router.post(
    "/check-sessions",
    response_model=CheckSessionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Open a check session",
)
def create_check_session(
    body: CheckSessionCreate, db: DbDep, principal: PrincipalDep
) -> CheckSessionOut:
    """Opened at the start of the flow, in **both** modes.

    This is what gives PRE-01 and CTX-01 something to attach to before — and, for a BP-only check,
    instead of — a sensor capture.
    """
    episode = load_episode(body.episode_id, principal, db)

    # The contraindication gate applies at the door: a patient who cannot get a trend should not be
    # walked through a check to be refused at the end of it.
    contraindication.assert_not_contraindicated(db, episode.patient_id)

    row = CheckSession(
        episode_id=episode.id,
        mode=body.mode,
        status=CheckSessionStatus.CREATED,
        started_at=datetime.now(tz=timezone.utc),
        synthetic=False,
    )
    db.add(row)
    db.flush()

    audit.record(db, principal=principal, action=AuditAction.CHECK_SESSION_CREATED, target=row.id)
    db.commit()

    log.info(
        "check_session_created",
        extra={"check_session_id": str(row.id), "mode": row.mode.value},
    )
    return _check_session_out(row)


@router.get(
    "/check-sessions/{session_id}",
    response_model=CheckSessionOut,
    summary="A check session",
)
def read_check_session(
    session_id: uuid.UUID, db: DbDep, principal: PrincipalDep
) -> CheckSessionOut:
    stored, _episode = _load_session(session_id, principal, db)
    return _check_session_out(stored)


@router.post(
    "/check-sessions/{session_id}/preconditions",
    response_model=PreconditionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Record PRE-01 for a check",
)
def record_preconditions(
    session_id: uuid.UUID, body: PreconditionCreate, db: DbDep, principal: PrincipalDep
) -> PreconditionOut:
    """PRE-01's five answers.

    Append-only, like the context: it describes the patient's state before one measurement.
    ``is_ready`` is derived here rather than accepted, so a client cannot claim readiness while
    reporting that it is not.
    """
    stored, _episode = _load_session(session_id, principal, db)

    is_ready = (
        body.rested_5_min
        and not body.recent_activity_30_min
        and not body.recent_caffeine_30_min
        and not body.recent_nicotine_30_min
        and not body.needs_restroom
    )

    row = Precondition(
        check_session_id=stored.id,
        recorded_at=datetime.now(tz=timezone.utc),
        rested_5_min=body.rested_5_min,
        recent_activity_30_min=body.recent_activity_30_min,
        recent_caffeine_30_min=body.recent_caffeine_30_min,
        recent_nicotine_30_min=body.recent_nicotine_30_min,
        needs_restroom=body.needs_restroom,
        is_ready=is_ready,
        synthetic=False,
    )
    db.add(row)
    db.flush()

    audit.record(
        db, principal=principal, action=AuditAction.PRECONDITIONS_RECORDED, target=row.id
    )
    db.commit()

    log.info(
        "preconditions_recorded",
        extra={"check_session_id": str(stored.id), "is_ready": is_ready},
    )

    return PreconditionOut(
        id=row.id,
        check_session_id=row.check_session_id,
        recorded_at=row.recorded_at,
        rested_5_min=row.rested_5_min,
        recent_activity_30_min=row.recent_activity_30_min,
        recent_caffeine_30_min=row.recent_caffeine_30_min,
        recent_nicotine_30_min=row.recent_nicotine_30_min,
        needs_restroom=row.needs_restroom,
        is_ready=row.is_ready,
        synthetic=row.synthetic,
    )


def _latest_precondition(db, check_session_id: uuid.UUID) -> Precondition | None:
    return db.execute(
        select(Precondition)
        .where(Precondition.check_session_id == check_session_id)
        .order_by(desc(Precondition.recorded_at), desc(Precondition.id))
        .limit(1)
    ).scalar_one_or_none()


def _context_out(row: SessionContext) -> SessionContextOut:
    return SessionContextOut(
        id=row.id,
        session_id=row.check_session_id,
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
        .where(SessionContext.check_session_id == session_id)
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
        check_session_id=stored.id,
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


def _build_features(
    db,
    check: CheckSession,
    context: SessionContext | None,
    precondition: Precondition | None,
) -> InsightFeatures:
    """Assemble what the matrix reads. All IO happens here; `evaluate` does none."""
    # The sensor capture belonging to this check, if there is one.
    capture = db.execute(
        select(MeasurementSession)
        .where(MeasurementSession.check_session_id == check.id)
        .order_by(desc(MeasurementSession.started_at))
        .limit(1)
    ).scalar_one_or_none()

    estimate = capture.estimate if capture is not None else None

    # The reference is the cuff reading the active calibration was anchored to. Invariant 1: this
    # is the only place a pressure value can come from, and it is never derived from the trend.
    reference: CuffReading | None = None
    if capture is not None and capture.calibration_id is not None:
        calibration = db.get(Calibration, capture.calibration_id)
        if calibration is not None and calibration.reference_cuff_reading_id is not None:
            reference = db.get(CuffReading, calibration.reference_cuff_reading_id)

    # A cuff reading taken during this check supersedes the reference for wording. For a BP-only
    # check it *is* the measurement, so the window opens at the check rather than at a capture.
    confirmed = db.execute(
        select(CuffReading)
        .where(
            CuffReading.episode_id == check.episode_id,
            CuffReading.taken_at >= check.started_at,
        )
        .order_by(desc(CuffReading.taken_at))
        .limit(1)
    ).scalar_one_or_none()

    quality = (capture.quality if capture is not None else None) or {}
    # "HR near resting" is not measured directly; motion is the proxy the capture reports, and it
    # is the same signal the comparability rows in section 24 are really about.
    motion = quality.get("motion_index")
    hr_near_resting = motion is None or float(motion) < 0.5

    return InsightFeatures(
        sensor_mode=check.mode is CheckMode.SENSOR,
        trend_direction=estimate.direction if estimate is not None else None,
        deviation_state=estimate.deviation_state if estimate is not None else None,
        reference_systolic=reference.systolic_mmhg if reference is not None else None,
        reference_diastolic=reference.diastolic_mmhg if reference is not None else None,
        confirmed_systolic=confirmed.systolic_mmhg if confirmed is not None else None,
        confirmed_diastolic=confirmed.diastolic_mmhg if confirmed is not None else None,
        hr_near_resting=hr_near_resting,
        # PRE-01's actual answers. Absent is treated as standard: the matrix's comparability rows
        # are about a *reported* confounder, and inventing one would refuse a check nobody said
        # anything wrong about.
        precondition_standard=precondition is None or precondition.is_ready,
        medication_status=(context.medication_status_today if context is not None else None),
        sleep_less_than_usual=context.sleep_less_than_usual if context is not None else False,
        stress_higher_than_usual=(
            context.stress_higher_than_usual if context is not None else False
        ),
        # A sensor check with no usable capture produced nothing. A BP-only check never has one,
        # and its measurement is the confirmed reading, so it is not "rejected" for lacking it.
        session_rejected=(
            check.mode is CheckMode.SENSOR
            and (capture is None or capture.status is SessionStatus.REJECTED)
        ),
    )


def _emr_context(db: DbDep, patient_id: uuid.UUID) -> dict | None:
    """A compact, de-identified profile summary for the LLM prompt.

    Age rather than date of birth, and nothing else that could pick this patient out of a crowd —
    see the module-level note in `services/llm_insight.py`. Returns `None` when the patient has no
    profile row yet, which the caller then omits from the prompt entirely rather than sending an
    empty object.
    """
    row = db.execute(
        select(PhrProfile).where(PhrProfile.patient_id == patient_id)
    ).scalar_one_or_none()
    if row is None:
        return None

    age = None
    if row.date_of_birth is not None:
        today = datetime.now(timezone.utc).date()
        dob = row.date_of_birth
        age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))

    out: dict = {}
    if age is not None:
        out["age_years"] = age
    if row.sex_assigned_at_birth is not None:
        out["sex"] = row.sex_assigned_at_birth.value
    if row.height_cm is not None:
        out["height_cm"] = row.height_cm
    if row.weight_kg is not None:
        out["weight_kg"] = row.weight_kg
    if row.hypertension_status is not None:
        out["hypertension_status"] = row.hypertension_status.value
    if row.taking_bp_medication is not None:
        out["taking_bp_medication"] = row.taking_bp_medication
    if row.conditions:
        out["conditions"] = list(row.conditions)
    return out or None


def _render(insight: Insight) -> dict:
    """Apply the language layer. Decision and wording stay separable."""
    return {
        **insight.to_json(),
        # `.get` with a floor, not `[]`. A code the language layer has not caught up with used to
        # raise `KeyError` here, and an unhandled exception in this handler is a 500 — so the whole
        # insight vanished rather than one sentence being wrong. The real guard is
        # `test_every_code_the_engine_can_emit_has_wording`, which fails the build on a missing
        # entry; this only decides what a patient sees if one ever reaches production.
        "hero": language.RESULT_STATE_WORDING.get(
            insight.result_state.value, language.RESULT_STATE_FALLBACK
        ),
        "next_best_step": language.PRIORITY_ACTION_WORDING.get(
            insight.priority_action_code.value, language.PRIORITY_ACTION_FALLBACK
        ),
        "context_chips": [
            language.CONTEXT_CODE_WORDING[code]
            for code in insight.context_codes
            if code in language.CONTEXT_CODE_WORDING
        ],
        "context_disclaimer": language.CONTEXT_DISCLAIMER,
        "notice": language.INSIGHT_NOTICE,
    }



def _estimated_pressure(db, check, settings):
    """This check's PTT against its calibration anchor, in mmHg.

    Everything here is read from rows that already exist: the session's own per-beat intervals,
    the calibration in force when it was captured, and the cuff reading that calibration was
    anchored to. Nothing is defaulted — a missing piece yields `None`, not a substitute.
    """
    capture = db.execute(
        select(MeasurementSession)
        .where(MeasurementSession.check_session_id == check.id)
        .order_by(desc(MeasurementSession.started_at))
        .limit(1)
    ).scalar_one_or_none()
    if capture is None or capture.calibration_id is None or not capture.ptt_ms:
        return None

    calibration = db.get(Calibration, capture.calibration_id)
    if calibration is None:
        return None
    anchor = db.get(CuffReading, calibration.reference_cuff_reading_id)
    if anchor is None:
        return None

    ptt_now = sum(capture.ptt_ms) / len(capture.ptt_ms)
    return pressure_estimate.estimate(
        ptt_now_ms=ptt_now,
        baseline_ptt_ms=calibration.baseline_mean_ms,
        calibration_systolic=anchor.systolic_mmhg,
        calibration_diastolic=anchor.diastolic_mmhg,
        calibration_established_at=calibration.established_at,
        settings=settings.pressure_estimate,
    )


@router.get(
    "/check-sessions/{session_id}/insight",
    summary="The deterministic insight for a check",
)
def read_insight(
    session_id: uuid.UUID,
    db: DbDep,
    principal: PrincipalDep,
    settings: SettingsDep,
    ai_consent: bool = False,
) -> dict:
    """Computed on read, stored nowhere.

    An insight is a function of rows that already exist, so recomputing it cannot drift from them
    — and there is no second copy to keep in step. The rule engine is pure; every read of the same
    session returns the same verdict.

    `ai_consent` adds exactly one field, `ai_commentary`, and touches nothing else in this
    response. Declined, unconfigured, or the call itself failing are the same outcome: the field
    is `None` and everything above it is identical to a plain read. See `services/llm_insight.py`
    for what "identical" is enforced by.
    """
    stored, episode = _load_session(session_id, principal, db)

    # The contraindication gate applies here too: an estimate withheld on the session detail must
    # not reappear wrapped in an insight.
    if contraindication.is_contraindicated(db, episode.patient_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=language.CONTRAINDICATED_PREGNANCY
        )

    context = _latest_context(db, stored.id)
    precondition = _latest_precondition(db, stored.id)
    insight = evaluate(_build_features(db, stored, context, precondition))
    rendered = _render(insight)

    # PTT -> mmHg, anchored on this patient's own cuff calibration.
    #
    # Computed on read and stored nowhere, which keeps `trend_estimate` free of a pressure column
    # and keeps this number from drifting out of step with the calibration it depends on: it is
    # recomputed from the same rows every time, or not produced at all. `estimate()` returns None
    # for a missing, stale or out-of-range anchor and the response simply carries nulls, leaving
    # the direction-only result the client already renders.
    estimated = _estimated_pressure(db, stored, settings)

    ai_commentary = None
    if ai_consent:
        ai_commentary = llm_insight.generate_commentary(
            insight=rendered,
            context=None if context is None else _context_out(context).as_features(),
            emr=_emr_context(db, episode.patient_id),
            settings=settings.llm_insight,
        )

    return {
        "session_id": str(stored.id),
        "synthetic": stored.synthetic,
        **rendered,
        # Null whenever an estimate could not honestly be produced — see `pressure_estimate`.
        "estimated_systolic": None if estimated is None else estimated.systolic_mmhg,
        "estimated_diastolic": None if estimated is None else estimated.diastolic_mmhg,
        "estimate_confidence": None if estimated is None else estimated.confidence,
        "estimate_calibration_age_days": (
            None if estimated is None else estimated.calibration_age_days
        ),
        "around_this_check": None if context is None else _context_out(context).as_features(),
        "ai_commentary": ai_commentary,
    }


# =============================================================================== state machine
#
# Section 31's diagram, written down once. Every transition route consults this table instead of
# comparing statuses inline: a state machine spread across three handlers is three places for the
# diagram and the code to drift apart.

#: Which statuses each transition may be entered from. A session already in the destination state
#: is handled before this is consulted.
_ALLOWED_FROM: dict[CheckSessionStatus, frozenset[CheckSessionStatus]] = {
    CheckSessionStatus.CAPTURE_PENDING: frozenset(
        {
            CheckSessionStatus.CREATED,
            CheckSessionStatus.REFERENCE_PENDING,
            CheckSessionStatus.PRECHECK_PENDING,
            CheckSessionStatus.CONTEXT_PENDING,
        }
    ),
    CheckSessionStatus.PROCESSING: frozenset(
        {
            CheckSessionStatus.CREATED,
            CheckSessionStatus.REFERENCE_PENDING,
            CheckSessionStatus.PRECHECK_PENDING,
            CheckSessionStatus.CONTEXT_PENDING,
            CheckSessionStatus.CAPTURE_PENDING,
        }
    ),
    CheckSessionStatus.COMPLETED: frozenset(
        {CheckSessionStatus.PROCESSING, CheckSessionStatus.CAPTURE_PENDING}
    ),
    CheckSessionStatus.FAILED_QUALITY: frozenset({CheckSessionStatus.CAPTURE_PENDING}),
}

#: Section 31: Completed and FailedQuality both go to [*]. Abandoned is terminal for the same
#: reason even though the diagram does not draw it.
_TERMINAL: frozenset[CheckSessionStatus] = frozenset(
    {
        CheckSessionStatus.COMPLETED,
        CheckSessionStatus.FAILED_QUALITY,
        CheckSessionStatus.ABANDONED,
    }
)


def _state_out(row: CheckSession) -> CheckSessionStateOut:
    return CheckSessionStateOut(
        id=row.id,
        episode_id=row.episode_id,
        mode=row.mode,
        status=row.status,
        started_at=row.started_at,
        completed_at=row.completed_at,
        synthetic=row.synthetic,
    )


def _advance(db, principal, stored: CheckSession, to: CheckSessionStatus) -> CheckSessionStateOut:
    """Move a session along the section 31 machine, or refuse and say why.

    Idempotent by design: asking for the state a session is already in succeeds and changes
    nothing. A handset retrying after a dropped response is the ordinary case rather than the
    exceptional one, and a 409 there would strand a patient mid-flow with no way forward.
    """
    if stored.status is to:
        return _state_out(stored)

    if stored.status in _TERMINAL:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"this check is already {stored.status.value} and cannot be changed",
        )

    if stored.status not in _ALLOWED_FROM[to]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a check in {stored.status.value} cannot move to {to.value}",
        )

    stored.status = to
    if to is CheckSessionStatus.COMPLETED:
        stored.completed_at = datetime.now(tz=timezone.utc)

    db.flush()
    audit.record(
        db, principal=principal, action=AuditAction.CHECK_SESSION_ADVANCED, target=stored.id
    )
    db.commit()

    log.info(
        "check_session_advanced",
        extra={"check_session_id": str(stored.id), "to_status": to.value},
    )
    return _state_out(stored)


@router.post(
    "/check-sessions/{session_id}/capture",
    response_model=CheckSessionStateOut,
    summary="Report the outcome of one capture attempt",
)
def record_capture(
    session_id: uuid.UUID,
    body: CaptureIn,
    db: DbDep,
    principal: PrincipalDep,
    settings: SettingsDep,
) -> CheckSessionStateOut:
    """Section 17's three-state quality gate, recorded against the session.

    **This route carries no signal** (invariant 2). The handset ran its own gate — it has to, the
    verdict drives what the patient sees within a second of the capture ending — and reports which
    of the three states it reached. Derived per-beat intervals still travel by their own road,
    ``POST /v1/sessions``, which stays the only route that accepts them.

    A BP-only check has no capture, so asking for one is refused rather than quietly recorded:
    a bp_only session walked through the capture states would produce a history entry describing
    a measurement that never happened.
    """
    stored, _episode = _load_session(session_id, principal, db)

    if stored.mode is CheckMode.BP_ONLY:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a bp_only check has no sensor capture",
        )

    if body.accepted:
        return _advance(db, principal, stored, CheckSessionStatus.PROCESSING)

    # Section 17: SIG-02 offers another attempt, SIG-03 does not. The ceiling is a clinical-flow
    # threshold rather than an engineering constant, so it comes from config (invariant 10) and
    # matches the count the handset is showing the patient.
    if body.attempt_number >= settings.deviation.max_capture_attempts:
        return _advance(db, principal, stored, CheckSessionStatus.FAILED_QUALITY)

    return _advance(db, principal, stored, CheckSessionStatus.CAPTURE_PENDING)


@router.post(
    "/check-sessions/{session_id}/process",
    response_model=CheckSessionStateOut,
    summary="Move a check into processing",
)
def start_processing(
    session_id: uuid.UUID, body: ProcessIn, db: DbDep, principal: PrincipalDep
) -> CheckSessionStateOut:
    """PROC-01 and PROC-02.

    Carries nothing: whatever is being processed is already stored — the capture for a sensor
    check, the confirmed cuff reading for a BP-only one. The route exists so the session's status
    reflects where the patient actually is, which is what History reads back.
    """
    stored, _episode = _load_session(session_id, principal, db)
    return _advance(db, principal, stored, CheckSessionStatus.PROCESSING)


@router.post(
    "/check-sessions/{session_id}/complete",
    response_model=CheckSessionStateOut,
    summary="Close a check",
)
def complete_session(
    session_id: uuid.UUID, db: DbDep, principal: PrincipalDep
) -> CheckSessionStateOut:
    """The terminal transition. Stamps ``completed_at``.

    The insight is deliberately **not** generated here. It is a pure function of rows that already
    exist (``GET .../insight``), so computing and storing one at completion would create a second
    copy free to drift from the facts it summarises.
    """
    stored, _episode = _load_session(session_id, principal, db)
    return _advance(db, principal, stored, CheckSessionStatus.COMPLETED)
