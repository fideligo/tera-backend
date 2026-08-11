"""Patient-supplied clinical context (B2C pivot).

The intake form is the only source of medication, pregnancy and rhythm history once there is no
clinic behind the account. These schemas mirror the five fields on the handset exactly, so a field
added on one side has an obvious home on the other.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field, model_validator

from app.models.enums import PregnancyAnswer
from app.schemas.common import SyntheticFlag, TeraModel

#: A patient on more than this is not being modelled by a phone app. The bound exists so the
#: JSONB column cannot become a data channel, the same reasoning as the event payload's key limit.
MAX_MEDICATIONS = 32


class MedicationIn(TeraModel):
    name: str = Field(min_length=1, max_length=128)
    dose: str = Field(default="", max_length=128)


class PatientContextCreate(TeraModel):
    """The five intake fields.

    ``patient_id`` is deliberately absent: it comes from the token, so a patient cannot file
    context against somebody else's record by editing a request body.
    """

    last_regimen_change_date: datetime | None = None
    medications: list[MedicationIn] = Field(default_factory=list, max_length=MAX_MEDICATIONS)
    pregnant: PregnancyAnswer
    known_arrhythmia: bool

    last_clinic_systolic_mmhg: int | None = Field(default=None, ge=0, le=1000)
    last_clinic_diastolic_mmhg: int | None = Field(default=None, ge=0, le=1000)
    last_clinic_taken_on: datetime | None = None

    @model_validator(mode="after")
    def _clinic_bp_is_all_or_nothing(self) -> "PatientContextCreate":
        """A systolic with no date is not a reading. The database CHECK says the same thing."""
        present = {
            self.last_clinic_systolic_mmhg is not None,
            self.last_clinic_diastolic_mmhg is not None,
            self.last_clinic_taken_on is not None,
        }
        if len(present) != 1:
            raise ValueError(
                "last_clinic_* must be given together or omitted together: both numbers and the "
                "date it was taken"
            )
        if (
            self.last_clinic_systolic_mmhg is not None
            and self.last_clinic_diastolic_mmhg is not None
            and self.last_clinic_systolic_mmhg <= self.last_clinic_diastolic_mmhg
        ):
            raise ValueError("last_clinic_systolic_mmhg must be above last_clinic_diastolic_mmhg")
        return self


class PatientContextOut(SyntheticFlag, TeraModel):
    """The context in force. Append-only, so this is the most recent row, not the only one."""

    id: uuid.UUID
    patient_id: uuid.UUID
    recorded_at: datetime
    last_regimen_change_date: datetime | None
    medications: list[MedicationIn]
    pregnant: PregnancyAnswer
    known_arrhythmia: bool
    last_clinic_systolic_mmhg: int | None
    last_clinic_diastolic_mmhg: int | None
    last_clinic_taken_on: datetime | None
