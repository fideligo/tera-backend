"""PHR profile and per-session context (PM spec sections 28 and 30)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import Field, model_validator

from app.models.enums import (
    CheckMode,
    CheckSessionStatus,
    HypertensionStatus,
    MedicationStatus,
    MedicationStatusToday,
    SexAtBirth,
)
from app.schemas.common import SyntheticFlag, TeraModel

#: The spec's condition codes (section 28). A closed list, so a typo is a 422 rather than a row
#: nobody can query later.
CONDITION_CODES: frozenset[str] = frozenset(
    {
        "diabetes",
        "prediabetes",
        "ckd",
        "heart_disease",
        "previous_mi",
        "stroke",
        "tia",
        "heart_failure",
        "sleep_apnea",
        "high_cholesterol",
        "high_uric_acid",
        "gout",
    }
)

#: Contextual symptoms. Deliberately **not** the invariant 8 red-flag list: those terminate a
#: session before capture, offline, and one arriving here would be arriving too late to act on.
CONTEXT_SYMPTOM_CODES: frozenset[str] = frozenset(
    {"headache", "dizziness", "palpitations", "fatigue", "swelling"}
)

#: Sanity bounds, not clinical thresholds. They catch a slipped decimal point, and a value inside
#: them is not a judgement about anybody. Mirrored as DB CHECKs.
MIN_HEIGHT_CM, MAX_HEIGHT_CM = 50.0, 250.0
MIN_WEIGHT_KG, MAX_WEIGHT_KG = 10.0, 400.0


class PhrProfilePatch(TeraModel):
    """`PATCH /v1/profile`. Every field optional — that is what makes it a patch.

    ``patient_id`` is deliberately absent: it comes from the token, so a profile cannot be edited
    against somebody else's record by changing a request body.
    """

    date_of_birth: date | None = None
    sex_assigned_at_birth: SexAtBirth | None = None
    height_cm: float | None = Field(default=None, ge=MIN_HEIGHT_CM, le=MAX_HEIGHT_CM)
    weight_kg: float | None = Field(default=None, ge=MIN_WEIGHT_KG, le=MAX_WEIGHT_KG)
    hypertension_status: HypertensionStatus | None = None
    taking_bp_medication: bool | None = None
    conditions: list[str] | None = Field(default=None, max_length=len(CONDITION_CODES))

    family_bp_history: str | None = None
    physical_activity: str | None = None
    smoking_status: str | None = None
    usual_sleep_hours: str | None = None
    usual_stress_level: str | None = None
    alcohol_frequency: str | None = None
    pregnancy_hypertension_history: str | None = None
    family_early_cardiac_history: bool | None = None
    medications: list[dict[str, Any]] | None = None

    @model_validator(mode="after")
    def _check(self) -> "PhrProfilePatch":
        if self.date_of_birth is not None and self.date_of_birth > date.today():
            raise ValueError("date_of_birth cannot be in the future")

        if self.conditions is not None:
            unknown = sorted(set(self.conditions) - CONDITION_CODES)
            if unknown:
                raise ValueError(
                    f"unknown condition code(s): {', '.join(unknown)}. "
                    f"Allowed: {', '.join(sorted(CONDITION_CODES))}"
                )
        return self


class PhrProfileOut(SyntheticFlag, TeraModel):
    """The profile as stored.

    **No BMI.** The spec forbids deriving one and invariant 6 forbids the class of thing: height
    and weight are returned as given and never combined.
    """

    patient_id: uuid.UUID
    date_of_birth: date | None
    sex_assigned_at_birth: SexAtBirth | None
    height_cm: float | None
    weight_kg: float | None
    hypertension_status: HypertensionStatus | None
    taking_bp_medication: bool | None
    conditions: list[str]
    family_bp_history: str | None
    physical_activity: str | None
    smoking_status: str | None
    usual_sleep_hours: str | None
    usual_stress_level: str | None
    alcohol_frequency: str | None
    pregnancy_hypertension_history: str | None
    family_early_cardiac_history: bool | None
    medications: list[dict[str, Any]] | None
    updated_at: datetime


class SessionContextPatch(TeraModel):
    """`PATCH /v1/check-sessions/{id}/context` — CTX-01 for one check.

    A patch by the spec's naming, an **insert** by its storage: `session_context` is append-only,
    so a correction supersedes rather than overwrites. What the patient reported around a past
    measurement is a fact about that moment.
    """

    sleep_less_than_usual: bool = False
    stress_higher_than_usual: bool = False
    feeling_unwell: bool = False
    symptoms: list[str] = Field(default_factory=list, max_length=len(CONTEXT_SYMPTOM_CODES))
    medication_status_today: MedicationStatusToday = MedicationStatusToday.NOT_SURE

    @model_validator(mode="after")
    def _known_symptoms(self) -> "SessionContextPatch":
        unknown = sorted(set(self.symptoms) - CONTEXT_SYMPTOM_CODES)
        if unknown:
            raise ValueError(
                f"unknown symptom code(s): {', '.join(unknown)}. "
                f"Allowed: {', '.join(sorted(CONTEXT_SYMPTOM_CODES))}"
            )
        return self


class SessionContextOut(SyntheticFlag, TeraModel):
    """The context in force for a session. The latest row, not the only one."""

    id: uuid.UUID
    session_id: uuid.UUID
    recorded_at: datetime
    sleep_less_than_usual: bool
    stress_higher_than_usual: bool
    feeling_unwell: bool
    symptoms: list[str]
    medication_status_today: MedicationStatusToday

    def as_features(self) -> dict[str, Any]:
        """The shape the rule engine reads."""
        return {
            "sleep_less_than_usual": self.sleep_less_than_usual,
            "stress_higher_than_usual": self.stress_higher_than_usual,
            "feeling_unwell": self.feeling_unwell,
            "symptoms": list(self.symptoms),
            "medication_status_today": self.medication_status_today.value,
        }


class CheckSessionCreate(TeraModel):
    """`POST /v1/check-sessions`. Opened at the start of the flow, in both modes."""

    episode_id: uuid.UUID
    mode: CheckMode


class CheckSessionOut(SyntheticFlag, TeraModel):
    id: uuid.UUID
    episode_id: uuid.UUID
    mode: CheckMode
    status: CheckSessionStatus
    started_at: datetime
    completed_at: datetime | None


class PreconditionCreate(TeraModel):
    """PRE-01's five answers.

    ``is_ready`` is **not** accepted: it is derived on the server from the five, so a client cannot
    declare itself ready while reporting that it is not.
    """

    rested_5_min: bool
    recent_activity_30_min: bool
    recent_caffeine_30_min: bool
    recent_nicotine_30_min: bool
    needs_restroom: bool


class PreconditionOut(SyntheticFlag, TeraModel):
    id: uuid.UUID
    check_session_id: uuid.UUID
    recorded_at: datetime
    rested_5_min: bool
    recent_activity_30_min: bool
    recent_caffeine_30_min: bool
    recent_nicotine_30_min: bool
    needs_restroom: bool
    is_ready: bool


# --------------------------------------------------------------------------- medications
#
# Section 28 models medications as their own table because they are a *list* that changes over
# time, unlike the single-valued lifestyle answers folded into the profile.


class MedicationIn(TeraModel):
    """`POST /v1/medications` — PROF-04.

    ``status`` is not accepted on create: a medication you are adding is one you are taking. It
    changes through the stop route, which is the only transition that exists.
    """

    name: str = Field(min_length=1, max_length=128)
    dose: str = Field(min_length=1, max_length=64)
    frequency: str = Field(min_length=1, max_length=64)
    started_at: date | None = None

    @model_validator(mode="after")
    def _not_future(self) -> "MedicationIn":
        if self.started_at is not None and self.started_at > date.today():
            raise ValueError("started_at cannot be in the future")
        return self


class MedicationUpdate(TeraModel):
    """`POST /v1/medications/{id}` — a correction, field by field.

    A mistyped dose is a correction to what someone is taking now, not a new fact about a
    different moment, which is why `medication` is mutable where the clinical tables are not.
    """

    name: str | None = Field(default=None, min_length=1, max_length=128)
    dose: str | None = Field(default=None, min_length=1, max_length=64)
    frequency: str | None = Field(default=None, min_length=1, max_length=64)
    started_at: date | None = None


class MedicationOut(SyntheticFlag, TeraModel):
    id: uuid.UUID
    name: str
    dose: str
    frequency: str
    started_at: date | None
    last_changed_at: date | None
    status: MedicationStatus


# --------------------------------------------------------------------------- conditions


class ConditionsIn(TeraModel):
    """`PUT /v1/conditions` in the spec, POST here — the whole list, not a delta.

    Replacing wholesale is what the screen does: PROF-03 is a checklist, and a patient unticking
    something has to be able to say so. A delta API cannot express a removal without a second verb.
    """

    conditions: list[str] = Field(default_factory=list, max_length=len(CONDITION_CODES))

    @model_validator(mode="after")
    def _known(self) -> "ConditionsIn":
        unknown = sorted(set(self.conditions) - CONDITION_CODES)
        if unknown:
            raise ValueError(
                f"unknown condition code(s): {', '.join(unknown)}. "
                f"Allowed: {', '.join(sorted(CONDITION_CODES))}"
            )
        return self


class ConditionsOut(TeraModel):
    conditions: list[str]
    updated_at: datetime


class ProfileCompletionOut(TeraModel):
    """`GET /v1/profile/completion` — what PROF-01 renders as a progress meter.

    A count of sections answered, and **nothing derived from the answers themselves**. It says
    whether a field is filled, never whether the value in it is good or bad: invariant 6, and the
    spec's own instruction not to judge a patient by their BMI.
    """

    complete: bool
    completed_sections: list[str]
    missing_sections: list[str]
    percent: int = Field(ge=0, le=100)


# --------------------------------------------------------------------------- check session


class CaptureIn(TeraModel):
    """`POST /v1/check-sessions/{id}/capture` — the quality-gate outcome for one attempt.

    **No waveform, by construction** (invariant 2). The handset has already run its own gate and
    reports what happened: accepted, retry, or out of attempts. There is no field here that could
    carry a sample buffer, and `measurement_session` remains the only route by which a derived
    per-beat interval enters the system.
    """

    accepted: bool
    attempt_number: int = Field(ge=1, le=10)
    reason: str | None = Field(default=None, max_length=64)


class ProcessIn(TeraModel):
    """`POST /v1/check-sessions/{id}/process` — move a session into processing.

    Carries nothing. What is being processed is already stored: the capture for a sensor check,
    the confirmed cuff reading for a BP-only one.
    """


class CheckSessionStateOut(SyntheticFlag, TeraModel):
    """A session and where the section 31 state machine now has it."""

    id: uuid.UUID
    episode_id: uuid.UUID
    mode: CheckMode
    status: CheckSessionStatus
    started_at: datetime
    completed_at: datetime | None
    #: Populated when a transition was refused, so a client can say why rather than showing a bare
    #: 409. Null on success.
    refused_reason: str | None = None
