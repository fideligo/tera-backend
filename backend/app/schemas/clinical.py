"""Cuff readings, calibrations and patient-reported events."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, field_validator, model_validator

from app.models.enums import CalibrationStatus, CuffSource, EventType
from app.schemas.common import SyntheticFlag, TeraModel
from app.services import language


class CuffReadingCreate(TeraModel):
    """POST /v1/cuff-readings.

    The only way a blood-pressure value enters the system (invariant 1), and it must have been
    confirmed by a person (``user_confirmed_at`` is NOT NULL by BUILD_SPEC 4.1).
    """

    episode_id: uuid.UUID
    systolic_mmhg: int = Field(ge=0, le=1000, description="Range-checked against config, not here.")
    diastolic_mmhg: int = Field(ge=0, le=1000)
    pulse_bpm: int | None = Field(default=None, ge=0, le=1000)
    #: 'photograph' is refused by the route: seven-segment OCR is out of scope (BUILD_SPEC 8) and
    #: accepting the value would imply a capability that does not exist.
    source: CuffSource = CuffSource.MANUAL_ENTRY
    ocr_confidence: float | None = Field(default=None, ge=0, le=1)
    taken_at: datetime
    user_confirmed_at: datetime
    #: Invariant 5 — a correction is a new row naming the row it corrects.
    corrects_id: uuid.UUID | None = None
    synthetic: bool = False


class CuffReadingOut(SyntheticFlag, TeraModel):
    """A cuff reading. The numerals are real and the unit is stated."""

    id: uuid.UUID
    episode_id: uuid.UUID
    systolic_mmhg: int
    diastolic_mmhg: int
    pulse_bpm: int | None
    unit: Literal["mmHg"] = "mmHg"
    source: CuffSource
    taken_at: datetime
    user_confirmed_at: datetime
    corrects_id: uuid.UUID | None = None
    badge: Literal[language.CUFF_BADGE] = language.CUFF_BADGE


class CalibrationCreate(TeraModel):
    """POST /v1/calibrations — establish or supersede a baseline.

    The client names the sessions; **the server computes the baseline**. Accepting a
    client-supplied ``baseline_mean_ms`` would let a handset write its own reference, and
    BUILD_SPEC 4.4 is explicit that the backend does not trust the client.
    """

    patient_id: uuid.UUID
    device_profile_id: uuid.UUID
    reference_cuff_reading_id: uuid.UUID
    session_ids: list[uuid.UUID] = Field(
        min_length=3,
        max_length=50,
        description="Accepted sessions on this device profile whose trimmed-mean PTT forms the "
        "baseline. At least three (BUILD_SPEC 4.1).",
    )
    synthetic: bool = False

    @field_validator("session_ids")
    @classmethod
    def _no_duplicates(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        """A session listed twice would inflate n_sessions without adding information."""
        if len(set(value)) != len(value):
            raise ValueError("session_ids must not contain duplicates")
        return value


class CalibrationOut(SyntheticFlag, TeraModel):
    id: uuid.UUID
    patient_id: uuid.UUID
    device_profile_id: uuid.UUID
    reference_cuff_reading_id: uuid.UUID
    baseline_mean_ms: float
    baseline_sd_ms: float
    n_sessions: int
    status: CalibrationStatus
    superseded_by_id: uuid.UUID | None
    established_at: datetime
    superseded_at: datetime | None
    source_session_ids: list[uuid.UUID] = Field(default_factory=list)


class EventCreate(TeraModel):
    """POST /v1/events — medication, symptom or red-flag report.

    ``payload`` is free-form JSONB so the handset can record what the patient selected without
    the API prescribing a clinical vocabulary it has no business defining.
    """

    episode_id: uuid.UUID
    event_type: EventType
    occurred_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)
    synthetic: bool = False

    @model_validator(mode="after")
    def _payload_is_bounded(self) -> "EventCreate":
        """Keep the payload a report, not a data channel.

        Invariant 2 in spirit: an unbounded JSONB column on an ingest endpoint is exactly the
        kind of place a waveform ends up.

        Note what this message does *not* say. An earlier version named the offending key —
        ``f"payload.{key} is too large"`` — which interpolates a client-supplied string into a
        validation error that is returned to the caller and passed through the log formatter.
        A payload key is client-controlled and can itself be clinical text, so a caller could
        have chosen one that put content into the error path. The client knows its own payload;
        the message states the rule and stops.
        """
        if len(self.payload) > 32:
            raise ValueError("payload may contain at most 32 keys")
        for value in self.payload.values():
            if isinstance(value, (list, dict)) and len(value) > 32:
                raise ValueError(
                    "a payload value is too large; events are reports, not series"
                )
        return self


class EventOut(SyntheticFlag, TeraModel):
    id: uuid.UUID
    episode_id: uuid.UUID
    event_type: EventType
    occurred_at: datetime
    recorded_at: datetime
    #: Invariant 8 — populated for red-flag events. The handset has already shown this locally;
    #: the field records what was shown, and its delivery is not a precondition for showing it.
    emergency_instruction: str | None = None
