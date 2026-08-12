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

@router.post(
    "/{session_id}/process",
    response_model=InterventionInsightOut,
    summary="Evaluate session against Intervention Condition Matrix (Sec 24)"
)
def process_check_session(
    session_id: uuid.UUID,
    db: DbDep,
    principal: PrincipalDep,
) -> InterventionInsightOut:
    """Implement PM Spec Section 24: Intervention Condition Matrix."""
    
    # Locate the CheckSession
    check_session = db.get(CheckSession, session_id)
    if not check_session:
        raise HTTPException(status_code=404, detail="CheckSession not found")
        
    # Get SessionContext
    session_ctx = db.execute(
        select(SessionContext)
        .where(SessionContext.check_session_id == session_id)
        .order_by(desc(SessionContext.recorded_at))
        .limit(1)
    ).scalar_one_or_none()
    
    missed_meds = session_ctx and session_ctx.medication_status_today == MedicationStatusToday.MISSED_OR_LATE
    stress_sleep = session_ctx and (session_ctx.stress_higher_than_usual or session_ctx.sleep_less_than_usual)
    
    # Shared rules for both eligible and non-eligible
    if missed_meds:
        return InterventionInsightOut(
            result_state="context_medication",
            hero_result="Medication note",
            what_this_means="Medication context is relevant to your reading.",
            next_best_step="Surface medication context, no dose recommendation",
            personalized_intervention="Make sure to take your medication as prescribed."
        )
    if stress_sleep:
        return InterventionInsightOut(
            result_state="context_lifestyle",
            hero_result="Lifestyle context",
            what_this_means="Associated context only. Stress and sleep can temporarily affect your pattern.",
            next_best_step="Add relevant lifestyle intervention",
            personalized_intervention="Try taking 5 minutes to breathe and relax."
        )

    # Mode specific evaluation
    if check_session.mode == CheckMode.SENSOR:
        # Get MeasurementSession
        measurement = db.execute(
            select(MeasurementSession)
            .where(MeasurementSession.check_session_id == session_id)
            .order_by(desc(MeasurementSession.started_at))
            .limit(1)
        ).scalar_one_or_none()
        
        trend = db.execute(
            select(TrendEstimate)
            .where(TrendEstimate.session_id == measurement.id)
        ).scalar_one_or_none() if measurement else None
        
        precond = db.execute(
            select(Precondition)
            .where(Precondition.check_session_id == session_id)
            .order_by(desc(Precondition.recorded_at))
            .limit(1)
        ).scalar_one_or_none()
        
        bp_ref = db.execute(
            select(BpReference)
            .where(BpReference.patient_id == principal.patient_id)
            .where(BpReference.status == "active")
            .limit(1)
        ).scalar_one_or_none()
        
        cuff_reading = None
        if bp_ref:
            cuff_reading = db.get(CuffReading, bp_ref.cuff_reading_id)
            
        elevated_ref = cuff_reading and (cuff_reading.systolic_mmhg >= 140 or cuff_reading.diastolic_mmhg >= 90)
        valid_condition = precond.is_ready if precond else True
        is_stable = trend and trend.direction == TrendDirection.STABLE
        is_single_change = trend and trend.deviation_state == DeviationState.POSSIBLE
        is_persistent = trend and trend.deviation_state == DeviationState.PERSISTENT
        
        if is_stable:
            if not elevated_ref:
                return InterventionInsightOut(
                    result_state="stable_normal",
                    hero_result="Your pattern looks good",
                    what_this_means="Pattern remains consistent with your <140/90 baseline.",
                    next_best_step="Continue monitoring"
                )
            else:
                return InterventionInsightOut(
                    result_state="stable_elevated",
                    hero_result="Trend is stable",
                    what_this_means="Sensor trend stable, but BP reference remains above threshold.",
                    next_best_step="Continue BP monitoring + follow-up if repeated"
                )
        elif is_single_change:
            if valid_condition:
                return InterventionInsightOut(
                    result_state="single_change_valid",
                    hero_result="Initial shift detected",
                    what_this_means="First change; persistence not established.",
                    next_best_step="Repeat later"
                )
            else:
                return InterventionInsightOut(
                    result_state="single_change_invalid",
                    hero_result="Check conditions",
                    what_this_means="Potential confounding context detected.",
                    next_best_step="Repeat under standardized condition"
                )
        elif is_persistent:
            if valid_condition:
                return InterventionInsightOut(
                    result_state="persistent_valid",
                    hero_result="Persistent shift",
                    what_this_means="Persistent BP-related change established.",
                    next_best_step="Confirm with fresh cuff BP"
                )
            else:
                return InterventionInsightOut(
                    result_state="persistent_invalid",
                    hero_result="Need cleaner measurement",
                    what_this_means="Need cleaner measurement before strong interpretation.",
                    next_best_step="Standardize + repeat"
                )
                
    else:
        # BP-Only Check
        cuff_reading = db.execute(
            select(CuffReading)
            .where(CuffReading.episode_id == check_session.episode_id)
            .order_by(desc(CuffReading.taken_at))
            .limit(1)
        ).scalar_one_or_none()
        
        elevated = cuff_reading and (cuff_reading.systolic_mmhg >= 140 or cuff_reading.diastolic_mmhg >= 90)
        
        if not elevated:
            return InterventionInsightOut(
                result_state="bp_normal",
                hero_result="Blood pressure is in range",
                what_this_means="Your confirmed BP is <140/90.",
                next_best_step="Continue routine monitoring"
            )
        else:
            return InterventionInsightOut(
                result_state="bp_elevated_isolated",
                hero_result="Elevated reading",
                what_this_means="Store, repeat/monitor, do not diagnose.",
                next_best_step="Log again later to see if it remains elevated"
            )

    return InterventionInsightOut(
        result_state="unknown",
        hero_result="Check completed",
        what_this_means="We've recorded your data.",
        next_best_step="Continue routine monitoring"
    )
