"""`/v1/profile` for the B2C schema (PM spec section 28's `phr_profiles`, `health_conditions`).

PATCH already existed; GET did not. Without it, `PATCH` wrote into `PhrProfileB2C` and nothing
could read it back — the profile screen had a save button with no way to confirm the save landed,
which is indistinguishable from a save that silently did nothing. This adds the read side and
returns the same shape from both, so a client never needs a second round trip to see what it just
wrote.

`conditions` was accepted nowhere. `HealthCondition` exists on the model and every other list
field here (medications, family history) already has a replace-the-list PATCH; conditions gets the
same treatment for the same reason — the PM spec's own PROF-03 is a checklist, not a delta.
"""

from datetime import date, datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select

from app.api.deps import DbDep, PrincipalDep
from app.models.recommended import (
    FamilyHistory,
    HealthCondition,
    LifestyleProfile,
    MedicationModel,
    PhrProfileB2C,
    User,
)

router = APIRouter(prefix="/profile", tags=["profile"])


class MedicationItem(BaseModel):
    name: str
    dose: Optional[str] = None
    frequency: Optional[str] = None
    status: str


class FamilyHistoryItem(BaseModel):
    condition: str
    relationship: str
    early_onset: Optional[bool] = None


class ProfileUpdateRequest(BaseModel):
    date_of_birth: Optional[date] = None
    sex_assigned_at_birth: Optional[str] = None
    height_cm: Optional[float] = None
    weight_kg: Optional[float] = None
    pregnancy_status: Optional[str] = None
    hypertension_status: Optional[str] = None
    taking_bp_medication: Optional[bool] = None

    physical_activity_level: Optional[str] = None
    smoking_status: Optional[str] = None
    usual_sleep_hours: Optional[str] = None
    usual_stress_level: Optional[str] = None

    # PROF-03. Section 28's closed code list is enforced on the handset; accepted as free strings
    # here, matching every other field on this route — none of them validate against an enum
    # either.
    conditions: Optional[List[str]] = None
    medications: Optional[List[MedicationItem]] = None
    family_history: Optional[List[FamilyHistoryItem]] = None


def _serialize(user: User) -> dict:
    profile = user.phr_profile
    lifestyle = user.lifestyle_profile
    return {
        "date_of_birth": profile.date_of_birth.isoformat() if profile and profile.date_of_birth else None,
        "sex_assigned_at_birth": profile.sex_assigned_at_birth if profile else None,
        "height_cm": profile.height_cm if profile else None,
        "weight_kg": profile.weight_kg if profile else None,
        "pregnancy_status": profile.pregnancy_status if profile else None,
        "hypertension_status": profile.hypertension_status if profile else None,
        "taking_bp_medication": profile.taking_bp_medication if profile else None,
        "physical_activity_level": lifestyle.physical_activity_level if lifestyle else None,
        "smoking_status": lifestyle.smoking_status if lifestyle else None,
        "usual_sleep_hours": lifestyle.usual_sleep_hours if lifestyle else None,
        "usual_stress_level": lifestyle.usual_stress_level if lifestyle else None,
        "conditions": [c.condition_code for c in user.health_conditions],
        "medications": [
            {"name": m.name, "dose": m.dose, "frequency": m.frequency, "status": m.status}
            for m in user.medications
        ],
        "family_history": [
            {
                "condition": fh.condition,
                "relationship": fh.relationship_type,
                "early_onset": fh.early_onset_boolean_optional,
            }
            for fh in user.family_history
        ],
        "onboarding_complete": user.onboarding_complete,
    }


def _load_user(db: DbDep, principal: PrincipalDep) -> User:
    user = db.execute(select(User).where(User.email == principal.subject)).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


# No GET here, deliberately. `phr.py` already serves `GET /v1/profile` and `POST /v1/profile`
# against the original schema, with plausibility bounds, a closed condition-code list and tenant
# scoping that this router does not have — and it is registered first, so a second GET here would
# never even be reached. Adding one was tried and reverted; see docs/decisions.md. The mobile
# client has been pointed at the tested route instead of duplicating validation here.
@router.patch("")
def update_profile(body: ProfileUpdateRequest, db: DbDep, principal: PrincipalDep):
    user = _load_user(db, principal)

    if not user.phr_profile:
        user.phr_profile = PhrProfileB2C(user_id=user.id)
    if not user.lifestyle_profile:
        user.lifestyle_profile = LifestyleProfile(user_id=user.id)

    if body.date_of_birth is not None:
        user.phr_profile.date_of_birth = body.date_of_birth
    if body.sex_assigned_at_birth is not None:
        user.phr_profile.sex_assigned_at_birth = body.sex_assigned_at_birth
    if body.height_cm is not None:
        user.phr_profile.height_cm = body.height_cm
    if body.weight_kg is not None:
        user.phr_profile.weight_kg = body.weight_kg
    if body.pregnancy_status is not None:
        user.phr_profile.pregnancy_status = body.pregnancy_status
    if body.hypertension_status is not None:
        user.phr_profile.hypertension_status = body.hypertension_status
    if body.taking_bp_medication is not None:
        user.phr_profile.taking_bp_medication = body.taking_bp_medication
    user.phr_profile.updated_at = datetime.now(tz=timezone.utc)

    if body.physical_activity_level is not None:
        user.lifestyle_profile.physical_activity_level = body.physical_activity_level
    if body.smoking_status is not None:
        user.lifestyle_profile.smoking_status = body.smoking_status
    if body.usual_sleep_hours is not None:
        user.lifestyle_profile.usual_sleep_hours = body.usual_sleep_hours
    if body.usual_stress_level is not None:
        user.lifestyle_profile.usual_stress_level = body.usual_stress_level

    # PROF-03: the whole list, not a delta — a patient unticking a condition has to be able to
    # say so, which a delta API cannot express without a second verb.
    if body.conditions is not None:
        for c in list(user.health_conditions):
            db.delete(c)
        db.flush()
        for code in body.conditions:
            db.add(HealthCondition(user_id=user.id, condition_code=code, status="reported"))

    if body.medications is not None:
        for m in list(user.medications):
            db.delete(m)
        db.flush()
        for m_in in body.medications:
            db.add(
                MedicationModel(
                    user_id=user.id,
                    name=m_in.name,
                    dose=m_in.dose,
                    frequency=m_in.frequency,
                    status=m_in.status,
                )
            )

    if body.family_history is not None:
        for fh in list(user.family_history):
            db.delete(fh)
        db.flush()
        for fh_in in body.family_history:
            db.add(
                FamilyHistory(
                    user_id=user.id,
                    condition=fh_in.condition,
                    relationship_type=fh_in.relationship,
                    early_onset_boolean_optional=fh_in.early_onset,
                )
            )

    user.onboarding_complete = True
    db.commit()

    # The session is `expire_on_commit=False`, so `user`'s relationships still hold their
    # pre-commit state — in particular the collections just replaced above. `expire` rather than
    # `refresh`: `refresh()` does not reliably reload collection relationships, and the read
    # right after a write is the one place a stale list would be most visible and most confusing.
    db.expire(user)

    return _serialize(user)
