from __future__ import annotations

import uuid
from typing import Literal
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select, desc

from app.api.deps import DbDep, PrincipalDep
from app.models.enums import TrendDirection, DeviationState, MedicationStatusToday, CheckMode
from app.models.clinical import (
    CheckSession,
    PatientContext,
    CuffReading,
    BpReference,
    SessionContext,
    Precondition,
)
from app.models.session import (
    MeasurementSession,
    TrendEstimate
)
from app.logging_config import get_logger

router = APIRouter(prefix="/check-sessions", tags=["check-sessions"])
log = get_logger(__name__)

class InterventionInsightOut(BaseModel):
    result_state: str
    hero_result: str
    what_this_means: str
    next_best_step: str
    personalized_intervention: str | None = None
    ai_commentary: str | None = None

from datetime import datetime, timezone
from app.models.recommended import SessionContextB2C, InsightB2C, SensorMeasurementB2C, BpReadingB2C

class ContextUpdateRequest(BaseModel):
    sleep_less_than_usual: bool
    stress_higher_than_usual: bool
    feeling_unwell: bool
    symptoms_json: str | None = None
    medication_status_today: str

@router.patch("/{session_id}/context")
def update_context(session_id: uuid.UUID, body: ContextUpdateRequest, db: DbDep, principal: PrincipalDep):
    b2c_ctx = SessionContextB2C(
        session_id=session_id,
        sleep_less_than_usual=body.sleep_less_than_usual,
        stress_higher_than_usual=body.stress_higher_than_usual,
        feeling_unwell=body.feeling_unwell,
        symptoms_json=body.symptoms_json,
        medication_status_today=body.medication_status_today
    )
    db.add(b2c_ctx)
    
    b2c_session = db.get(CheckSessionB2C, session_id)
    if b2c_session:
        b2c_session.status = "capture_pending"
        
    db.commit()
    return {"status": "success"}

class BpPayload(BaseModel):
    systolic: int
    diastolic: int
    pulse: int | None = None
    source: str

class ProcessSessionRequest(BaseModel):
    # Sensor data
    raw_scg_storage_ref: str | None = None
    raw_ppg_storage_ref: str | None = None
    scg: list[float] | None = None
    ppg: list[float] | None = None
    ptt_ms: float | None = None
    heart_rate_bpm: int | None = None
    # BP data
    blood_pressure: BpPayload | None = None
    ai_consent: bool | None = False

@router.post(
    "/{session_id}/process",
    response_model=InterventionInsightOut,
    summary="Evaluate session against Intervention Condition Matrix (Sec 24)"
)
def process_check_session(
    session_id: uuid.UUID,
    body: ProcessSessionRequest,
    db: DbDep,
    principal: PrincipalDep,
) -> InterventionInsightOut:
    """Implement PM Spec Section 24: Intervention Condition Matrix and process payload."""
    
    b2c_session = db.get(CheckSessionB2C, session_id)
    if not b2c_session:
        raise HTTPException(status_code=404, detail="CheckSession not found")
        
    b2c_session.status = "completed"
    b2c_session.completed_at = datetime.now(timezone.utc)
    
    user = db.execute(select(User).where(User.email == principal.subject)).scalar_one_or_none()
    
    if b2c_session.mode == "bp_only" and body.blood_pressure:
        bp_reading = BpReadingB2C(
            user_id=user.id,
            session_id_optional=session_id,
            systolic=body.blood_pressure.systolic,
            diastolic=body.blood_pressure.diastolic,
            pulse=body.blood_pressure.pulse,
            measured_at=datetime.now(timezone.utc),
            source_manual_or_ocr=body.blood_pressure.source or "manual",
            ocr_confidence_optional=None,
            user_confirmed=True,
            used_as_reference=False
        )
        db.add(bp_reading)
    elif b2c_session.mode == "sensor":
        sensor_measurement = SensorMeasurementB2C(
            session_id=session_id,
            raw_scg_storage_ref=body.raw_scg_storage_ref,
            raw_ppg_storage_ref=body.raw_ppg_storage_ref,
            ptt_ms=body.ptt_ms,
            heart_rate_bpm=body.heart_rate_bpm,
            capture_duration=10,
            algorithm_version="1.0.0"
        )
        db.add(sensor_measurement)

    # Rule Engine Logic (Section 24)
    bp = body.blood_pressure
    result_state = "unclassified"
    hero_result = "Check completed"
    what_this_means = "Vitals recorded"
    next_best_step = "Continue monitoring"
    personalized_intervention = ""
    
    if body.scg and body.ppg:
        import sys
        sys.path.append("app/ml")
        try:
            import contract as C
            cap = C.Capture(scg=body.scg, ppg=body.ppg, posture="seated", anchor=None)
            res = C.session(cap)
            if not body.heart_rate_bpm:
                body.heart_rate_bpm = res.get("hr_bpm")
        except Exception as e:
            log.error(f"ML Processing failed: {e}")
            
    context = db.execute(select(SessionContextB2C).where(SessionContextB2C.session_id == session_id)).scalar_one_or_none()
    precondition = db.execute(select(PreconditionB2C).where(PreconditionB2C.session_id == session_id)).scalar_one_or_none()
    
    if b2c_session.mode == "bp_only":
        if bp and bp.systolic < 140 and bp.diastolic < 90:
            result_state = "bp_normal"
            hero_result = "Continue routine monitoring"
            what_this_means = "BP is within normal range."
            next_best_step = "Continue monitoring"
        elif bp and (bp.systolic >= 140 or bp.diastolic >= 90):
            past_elevated = db.execute(select(BpReadingB2C).where(BpReadingB2C.user_id == user.id, BpReadingB2C.systolic >= 140)).all()
            if len(past_elevated) > 0:
                result_state = "bp_elevated_repeated"
                hero_result = "Follow-up becomes more relevant"
                what_this_means = "Repeated elevated readings."
                next_best_step = "Consult healthcare provider"
            else:
                result_state = "bp_elevated_isolated"
                hero_result = "Store, repeat/monitor, do not diagnose"
                what_this_means = "First isolated elevated reading."
                next_best_step = "Repeat later"
        
        if bp and bp.pulse and (bp.pulse < 50 or bp.pulse > 100) and context:
            personalized_intervention = "Explain reduced comparability if relevant"
        elif context and context.medication_status_today == "missed":
            personalized_intervention = "Surface medication context, no dose recommendation"
        elif context and (context.stress_higher_than_usual or context.sleep_less_than_usual):
            personalized_intervention = "1-2 relevant guideline-based recommendations"

    elif b2c_session.mode == "sensor":
        bp_ref = db.execute(select(BpReadingB2C).where(BpReadingB2C.user_id == user.id, BpReadingB2C.used_as_reference == True)).scalar_one_or_none()
        
        trend = "Stable"
        hr_condition = "resting/comparable"
        
        if body.heart_rate_bpm and body.heart_rate_bpm > 100:
            hr_condition = "HR unusually high"
        if precondition and (not precondition.rested_5_min or precondition.recent_activity_30_min):
            hr_condition = "non-standard precondition"
        if context and context.medication_status_today == "missed":
            hr_condition = "missed medication"
        elif context and (context.stress_higher_than_usual or context.sleep_less_than_usual):
            hr_condition = "stress/sleep context"
            
        if bp_ref and bp_ref.systolic < 140 and bp_ref.diastolic < 90:
            if trend == "Stable" and hr_condition == "resting/comparable":
                result_state = "bp_normal"
                hero_result = "Pattern remains consistent"
                what_this_means = "Continue monitoring"
                next_best_step = "Continue monitoring"
        elif bp_ref and (bp_ref.systolic >= 140 or bp_ref.diastolic >= 90):
            if trend == "Stable" and hr_condition == "resting/comparable":
                result_state = "bp_elevated_stable"
                hero_result = "Sensor trend stable, but BP reference remains above threshold"
                what_this_means = "Continue BP monitoring + follow-up if repeated"
                next_best_step = "Continue BP monitoring"
                
        if hr_condition == "HR unusually high":
            result_state = "hr_high"
            hero_result = "Less comparable"
            what_this_means = "HR unusually high"
            next_best_step = "Rest + repeat"
        elif hr_condition == "non-standard precondition":
            result_state = "precondition_invalid"
            hero_result = "Potential confounding"
            what_this_means = "Non-standard precondition"
            next_best_step = "Repeat under standardized condition"
        elif hr_condition == "missed medication":
            result_state = "medication_missed"
            hero_result = "Medication context is relevant"
            what_this_means = "No dose change advice"
            next_best_step = "Continue monitoring"
        elif hr_condition == "stress/sleep context":
            result_state = "lifestyle_context"
            hero_result = "Associated context only"
            what_this_means = "Stress/sleep context noted"
            next_best_step = "Add relevant lifestyle intervention"

    insight_out = InterventionInsightOut(
        result_state=result_state,
        hero_result=hero_result,
        what_this_means=what_this_means,
        next_best_step=next_best_step,
        personalized_intervention=personalized_intervention
    )
    
    if getattr(body, "ai_consent", False):
        try:
            import httpx
            import os
            
            phr_summary = "Patient Context: "
            if context:
                phr_summary += f"Stress high: {context.stress_higher_than_usual}, Sleep less: {context.sleep_less_than_usual}, Medication: {context.medication_status_today}. "
            if body.blood_pressure:
                phr_summary += f"BP: {body.blood_pressure.systolic}/{body.blood_pressure.diastolic}. "
            if body.heart_rate_bpm:
                phr_summary += f"HR: {body.heart_rate_bpm} BPM. "
                
            phr_summary += f"Insight state: {insight_out.result_state}, Next step: {insight_out.next_best_step}."
            
            nim_key = os.environ.get("NVIDIA_API_KEY", "dummy_key")
            headers = {
                "Authorization": f"Bearer {nim_key}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": "meta/llama3-70b-instruct",
                "messages": [
                    {"role": "system", "content": "You are a specialized medical AI assistant. Give a short, empathetic commentary for the patient based on their vitals."},
                    {"role": "user", "content": f"Patient data:\n{phr_summary}\nGenerate AI commentary:"}
                ],
                "max_tokens": 150
            }
            
            with httpx.Client() as client:
                res = client.post("https://integrate.api.nvidia.com/v1/chat/completions", headers=headers, json=payload, timeout=10.0)
                if res.status_code == 200:
                    insight_out.ai_commentary = res.json()["choices"][0]["message"]["content"].strip()
                else:
                    insight_out.ai_commentary = f"AI generation failed (status {res.status_code}). Response: {res.text}"
        except Exception as e:
            insight_out.ai_commentary = f"AI Error: {str(e)}"
    
    # Store the insight
    db.add(InsightB2C(
        session_id=session_id,
        result_state=insight_out.result_state,
        interpretation_code=insight_out.hero_result,
        priority_action_code=insight_out.next_best_step,
        recommendation_codes_json=insight_out.what_this_means,
        monitoring_plan_code=insight_out.personalized_intervention,
        followup_code="none"
    ))
    
    db.commit()
    return insight_out

@router.get("/{session_id}/insight", response_model=InterventionInsightOut)
def get_insight(session_id: uuid.UUID, db: DbDep, ai_consent: bool = False):
    insight = db.execute(select(InsightB2C).where(InsightB2C.session_id == session_id)).scalar_one_or_none()
    if not insight:
        raise HTTPException(404, "Insight not found")
    
    insight_out = InterventionInsightOut(
        result_state=insight.result_state,
        hero_result=insight.interpretation_code or "Check completed",
        what_this_means=insight.recommendation_codes_json or "",
        next_best_step=insight.priority_action_code or "Continue monitoring",
        personalized_intervention=insight.monitoring_plan_code or ""
    )
    
    if ai_consent:
        try:
            import httpx
            import os
            nim_key = os.environ.get("NVIDIA_API_KEY", "dummy_key")
            headers = {
                "Authorization": f"Bearer {nim_key}",
                "Content-Type": "application/json"
            }
            payload = {
                "model": "meta/llama3-70b-instruct",
                "messages": [
                    {"role": "system", "content": "You are a specialized medical AI assistant. Give a short, empathetic commentary for the patient based on their vitals."},
                    {"role": "user", "content": f"Patient data: {insight.result_state}, {insight.interpretation_code}. Generate short AI commentary:"}
                ],
                "max_tokens": 150
            }
            with httpx.Client() as client:
                res = client.post("https://integrate.api.nvidia.com/v1/chat/completions", headers=headers, json=payload, timeout=10.0)
                if res.status_code == 200:
                    insight_out.ai_commentary = res.json()["choices"][0]["message"]["content"].strip()
                else:
                    insight_out.ai_commentary = f"AI generation failed (status {res.status_code}). Response: {res.text}"
        except Exception as e:
            insight_out.ai_commentary = f"AI Error: {str(e)}"
            
    return insight_out

from app.models.recommended import CheckSessionB2C, PreconditionB2C, User

class CreateCheckSessionRequest(BaseModel):
    mode: str
    device_id: uuid.UUID

class CreateCheckSessionResponse(BaseModel):
    session_id: uuid.UUID

@router.post("", response_model=CreateCheckSessionResponse)
def create_check_session(body: CreateCheckSessionRequest, db: DbDep, principal: PrincipalDep):
    user = db.execute(select(User).where(User.email == principal.subject)).scalar_one_or_none()
    if not user:
        raise HTTPException(404, "User not found")
        
    b2c_session = CheckSessionB2C(
        user_id=user.id,
        device_id=body.device_id,
        mode=body.mode,
        status="created"
    )
    db.add(b2c_session)
    db.commit()
    return {"session_id": b2c_session.id}

class PreconditionUpdateRequest(BaseModel):
    rested_5_min: bool
    recent_activity_30_min: bool
    recent_caffeine_30_min: bool
    recent_nicotine_30_min: bool
    needs_restroom: bool
    status: str

@router.patch("/{session_id}/preconditions")
def update_preconditions(session_id: uuid.UUID, body: PreconditionUpdateRequest, db: DbDep, principal: PrincipalDep):
    b2c_precond = PreconditionB2C(
        session_id=session_id,
        rested_5_min=body.rested_5_min,
        recent_activity_30_min=body.recent_activity_30_min,
        recent_caffeine_30_min=body.recent_caffeine_30_min,
        recent_nicotine_30_min=body.recent_nicotine_30_min,
        needs_restroom=body.needs_restroom,
        status=body.status
    )
    db.add(b2c_precond)
    
    b2c_session = db.get(CheckSessionB2C, session_id)
    if b2c_session:
        b2c_session.status = "context_pending"
        
    db.commit()
    return {"status": "success"}
