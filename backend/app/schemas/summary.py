"""Clinician exception summary (GET /v1/episodes/{id}/summary).

BUILD_SPEC 5.3: designed to be scanned in under two minutes. So this is an *exception* summary —
it reports what departed from expectation, not everything that happened.

Invariant 6 constrains every field: this is a record of what was measured and reported. It
contains no interpretation of what any of it means clinically, no diagnosis, and nothing
resembling a recommendation.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from app.models.enums import CalibrationStatus, DeviationState, RejectionReason, TrendDirection
from app.schemas.common import SyntheticFlag, TeraModel
from app.services import language


class SummaryCuffReading(TeraModel):
    """Cuff-confirmed readings — the only mmHg in this document."""

    id: uuid.UUID
    systolic_mmhg: int
    diastolic_mmhg: int
    pulse_bpm: int | None
    unit: str = "mmHg"
    taken_at: datetime
    user_confirmed_at: datetime
    corrects_id: uuid.UUID | None = None
    synthetic: bool = False
    cuff_badge: str = language.CUFF_BADGE


class SummaryNotableChange(TeraModel):
    """An estimate that departed from the patient's baseline.

    Reported in baseline standard deviations, with the estimate badge attached, because it is
    not a measurement (invariant 1).
    """

    session_id: uuid.UUID
    occurred_at: datetime
    direction: TrendDirection
    magnitude_sd: float
    confidence: float
    deviation_state: DeviationState
    cuff_requested: bool
    synthetic: bool = False
    estimate_badge: str = language.ESTIMATE_BADGE


class SummaryRejectedSession(TeraModel):
    """Invariant 3 — the clinician summary reports rejected sessions."""

    session_id: uuid.UUID
    occurred_at: datetime
    rejection_reason: RejectionReason
    reason_text: str
    synthetic: bool = False


class SummaryEvent(TeraModel):
    occurred_at: datetime
    payload: dict[str, Any]
    synthetic: bool = False


class SummaryCalibrationVersion(TeraModel):
    """Invariant 4 — the version history, so supersession is visible rather than implied."""

    id: uuid.UUID
    device_profile_id: uuid.UUID
    device_model: str
    baseline_mean_ms: float
    baseline_sd_ms: float
    n_sessions: int
    status: CalibrationStatus
    established_at: datetime
    superseded_at: datetime | None
    superseded_by_id: uuid.UUID | None
    reference_cuff_reading_id: uuid.UUID
    synthetic: bool = False


class SummarySessionYield(TeraModel):
    """System indicators, not physiological ones (BUILD_SPEC 5.1 reserves warning treatment
    for system states)."""

    sessions_submitted: int
    sessions_completed: int
    sessions_rejected: int
    estimates_produced: int
    completion_rate: float = Field(ge=0, le=1)
    rejections_by_reason: dict[str, int] = Field(default_factory=dict)


class SummaryMedicationLog(TeraModel):
    """A factual count of what the patient logged.

    Invariant 6: this is not an adherence judgement and carries no recommendation. It reports
    how many medication events were recorded and on how many distinct days.
    """

    events_logged: int
    days_with_a_log: int
    episode_days_elapsed: int
    first_logged_at: datetime | None = None
    last_logged_at: datetime | None = None


class ClinicianSummaryOut(SyntheticFlag, TeraModel):
    """The whole document."""

    episode_id: uuid.UUID
    patient_pseudonym: str
    #: Null for a self-registered B2C patient, who has no clinic behind the account.
    clinic_id: str | None
    started_at: datetime
    ended_at: datetime | None
    generated_at: datetime
    protocol_params: dict[str, Any]

    cuff_readings: list[SummaryCuffReading] = Field(default_factory=list)
    notable_changes: list[SummaryNotableChange] = Field(default_factory=list)
    rejected_sessions: list[SummaryRejectedSession] = Field(default_factory=list)
    symptom_events: list[SummaryEvent] = Field(default_factory=list)
    red_flag_events: list[SummaryEvent] = Field(default_factory=list)
    medication_log: SummaryMedicationLog
    calibration_history: list[SummaryCalibrationVersion] = Field(default_factory=list)
    session_yield: SummarySessionYield

    days_since_last_cuff_reading: int | None = None
    active_calibration_age_days: int | None = None
    unsynchronised_sessions: int = Field(
        default=0,
        description="Sessions whose upload lagged capture by more than a day — a system "
        "indicator, not a clinical one.",
    )


class EpisodeListItem(TeraModel):
    """Row in the clinician episode list (BUILD_SPEC 5.3 screen 2)."""

    episode_id: uuid.UUID
    patient_pseudonym: str
    #: Null for a self-registered B2C patient, who has no clinic behind the account.
    clinic_id: str | None
    started_at: datetime
    ended_at: datetime | None
    synthetic: bool
    sessions_submitted: int
    completion_rate: float
    days_since_last_cuff_reading: int | None
    active_calibration_age_days: int | None
    has_active_calibration: bool
    open_cuff_requests: int
    red_flag_count: int


class EpisodeListOut(TeraModel):
    episodes: list[EpisodeListItem] = Field(default_factory=list)
    contains_synthetic_data: bool = False
    synthetic_notice: str | None = None
