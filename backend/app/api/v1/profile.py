from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from datetime import date
from sqlalchemy import select

from app.api.deps import DbDep, PrincipalDep
from app.models.recommended import (
    User,
    PhrProfileB2C,
    MedicationModel,
    LifestyleProfile,
    FamilyHistory,
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
    
    medications: Optional[List[MedicationItem]] = None
    family_history: Optional[List[FamilyHistoryItem]] = None

@router.patch("")
def update_profile(body: ProfileUpdateRequest, db: DbDep, principal: PrincipalDep):
    user = db.execute(select(User).where(User.email == principal.subject)).scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
        
    if not user.phr_profile:
        user.phr_profile = PhrProfileB2C(user_id=user.id)
    if not user.lifestyle_profile:
        user.lifestyle_profile = LifestyleProfile(user_id=user.id)
        
    if body.date_of_birth is not None: user.phr_profile.date_of_birth = body.date_of_birth
    if body.sex_assigned_at_birth is not None: user.phr_profile.sex_assigned_at_birth = body.sex_assigned_at_birth
    if body.height_cm is not None: user.phr_profile.height_cm = body.height_cm
    if body.weight_kg is not None: user.phr_profile.weight_kg = body.weight_kg
    if body.pregnancy_status is not None: user.phr_profile.pregnancy_status = body.pregnancy_status
    if body.hypertension_status is not None: user.phr_profile.hypertension_status = body.hypertension_status
    if body.taking_bp_medication is not None: user.phr_profile.taking_bp_medication = body.taking_bp_medication
    
    if body.physical_activity_level is not None: user.lifestyle_profile.physical_activity_level = body.physical_activity_level
    if body.smoking_status is not None: user.lifestyle_profile.smoking_status = body.smoking_status
    if body.usual_sleep_hours is not None: user.lifestyle_profile.usual_sleep_hours = body.usual_sleep_hours
    if body.usual_stress_level is not None: user.lifestyle_profile.usual_stress_level = body.usual_stress_level
    
    if body.medications is not None:
        for m in user.medications:
            db.delete(m)
        user.medications = []
        for m_in in body.medications:
            m = MedicationModel(user_id=user.id, name=m_in.name, dose=m_in.dose, frequency=m_in.frequency, status=m_in.status)
            db.add(m)
            
    if body.family_history is not None:
        for fh in user.family_history:
            db.delete(fh)
        user.family_history = []
        for fh_in in body.family_history:
            fh = FamilyHistory(user_id=user.id, condition=fh_in.condition, relationship_type=fh_in.relationship, early_onset_boolean_optional=fh_in.early_onset)
            db.add(fh)
            
    user.onboarding_complete = True
    db.commit()
    
    return {"status": "success"}
