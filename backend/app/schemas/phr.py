"""PHR profile and per-session context (PM spec sections 28 and 30)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import Field, model_validator

from app.models.enums import (
    HypertensionStatus,
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
