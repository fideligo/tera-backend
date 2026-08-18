"""Session ingest and the trend estimate response.

**This module is where invariant 1 is enforced at the API boundary.** ``TrendEstimateOut`` has no
pressure field, cannot acquire one without failing
``test_no_pressure_value_in_any_estimate_response``, and carries a non-optional badge saying what
it is not.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import Field, field_validator

from app.models.enums import DeviationState, Posture, RejectionReason, SessionStatus, TrendDirection
from app.schemas.common import SyntheticFlag, TeraModel
from app.services import language

#: Hard cap on the submitted array, mirrored from PlausibilitySettings so an oversized payload is
#: refused by the parser before it reaches the database. The configured limit is applied again in
#: the plausibility gate, which is where the specific 422 message comes from.
_PTT_PARSE_CEILING = 10_000


class NonceOut(TeraModel):
    """Response of POST /v1/sessions/nonce."""

    nonce: str
    expires_at: datetime


class SessionQuality(TeraModel):
    """Achieved rates and signal-quality metrics from the on-device gate.

    Invariant 2: these are summary statistics. There is deliberately no field here for a frame
    series, an intensity trace or a sample buffer, and ``extra="forbid"`` means adding one to a
    payload is a 422 rather than a silently ignored key.
    """

    accel_rate_hz: float = Field(gt=0, le=10_000)
    camera_fps: float = Field(gt=0, le=1_000)
    dropped_frame_pct: float = Field(ge=0, le=100)
    snr_db: float = Field(ge=-100, le=100)
    motion_index: float = Field(ge=0, le=1, description="0 is still, 1 is unusable movement.")
    clock_offset_ms: float | None = Field(default=None, ge=-10_000, le=10_000)

    #: Which accelerometer axis the intervals were derived from: ``z``, ``x`` or ``y``.
    #:
    #: The aortic-valve signature sits on the axis normal to the chest wall, and which physical
    #: axis that is depends on how the patient held the phone. The handset now tries all three and
    #: keeps whichever passes its gate with the tightest spread (the ML handover's ``run_best_axis``,
    #: ported into `signal_pipeline.dart`), so this records which one actually carried the signal.
    #:
    #: Worth storing rather than discarding: a run of captures that only ever worked on ``x`` is a
    #: fact about how the phone is being held, and it is the difference between fixing an
    #: instruction and re-deriving a signal chain. Optional, so an older handset that does not send
    #: it still submits.
    scg_axis: Literal["x", "y", "z"] | None = None


class SessionSubmit(TeraModel):
    """POST /v1/sessions body.

    ``session_id`` is generated on the device and must equal the ``Idempotency-Key`` header.
    """

    session_id: uuid.UUID
    episode_id: uuid.UUID
    device_profile_id: uuid.UUID
    model_version: str = Field(max_length=64)
    started_at: datetime
    posture: Posture
    status: SessionStatus
    rejection_reason: RejectionReason | None = None
    n_beats_total: int = Field(ge=0, le=100_000)
    n_beats_usable: int = Field(ge=0, le=100_000)
    ptt_ms: list[float] = Field(
        default_factory=list,
        max_length=_PTT_PARSE_CEILING,
        description="One derived interval per usable beat, in milliseconds. The deepest "
        "granularity the API accepts (invariant 2) — never a waveform.",
    )
    quality: SessionQuality
    #: Optional. When present the server checks it against the calibration it resolves for
    #: ``started_at``; a disagreement is a 422 rather than a silent server-side override.
    calibration_id: uuid.UUID | None = None
    synthetic: bool = Field(default=False)

    @field_validator("rejection_reason")
    @classmethod
    def _reason_matches_status(
        cls, value: RejectionReason | None, info
    ) -> RejectionReason | None:
        """Invariant 3 — status and reason must agree, checked before the database sees it."""
        status = info.data.get("status")
        if status is SessionStatus.REJECTED and value is None:
            raise ValueError("a rejected session must carry a rejection_reason")
        if status is SessionStatus.COMPLETED and value is not None:
            raise ValueError("a completed session must not carry a rejection_reason")
        return value


class TrendEstimateOut(TeraModel):
    """A trend estimate. **Never a blood-pressure reading.**

    Invariant 1: there is no systolic field, no diastolic field, no mmHg value and no unit that
    could be mistaken for one. ``magnitude_sd`` counts standard deviations of the patient's own
    baseline.
    """

    calibration_id: uuid.UUID
    direction: TrendDirection
    magnitude_sd: float = Field(
        ge=0,
        description="Standard deviations of this patient's own baseline PTT. Not a pressure "
        "and does not convert to one.",
    )
    confidence: float = Field(gt=0, lt=1)
    deviation_state: DeviationState
    interpretation: str = Field(
        description="Plain-language placement relative to the patient's usual range."
    )
    #: Not optional and not dismissible (BUILD_SPEC 5.2). It ships with the data so a client
    #: cannot render the estimate without it.
    badge: Literal[language.ESTIMATE_BADGE] = language.ESTIMATE_BADGE
    magnitude_notice: str = language.MAGNITUDE_NOTICE
    confidence_notice: str = language.CONFIDENCE_NOTICE


class NextAction(TeraModel):
    """What the patient should do next.

    Invariant 6 keeps this to measurement logistics: take another spot check, take a cuff
    reading, seek emergency care. Never a medication instruction, never a clinical judgement.
    """

    kind: Literal[
        "none",
        "repeat_session_suggested",
        "cuff_reading_requested",
        "seek_emergency_care",
    ]
    message: str


class RejectionOut(TeraModel):
    """Invariant 3 — a rejected session is part of the record, with its reason."""

    reason: RejectionReason
    message: str
    badge: Literal[language.REJECTED_BADGE] = language.REJECTED_BADGE


class SessionAccepted(SyntheticFlag, TeraModel):
    """201 (or replayed 409) response for POST /v1/sessions."""

    session_id: uuid.UUID
    status: SessionStatus
    #: Null whenever no estimate was produced — a rejected session, or no calibration in force.
    #: Invariant 7: the absence of an estimate is the correct output in an ambiguous state, not
    #: an error to be worked around.
    trend: TrendEstimateOut | None = None
    rejection: RejectionOut | None = None
    action: NextAction


class SessionDetailOut(SyntheticFlag, TeraModel):
    """Patient-facing session detail (drives the Phase 2 session-detail screen)."""

    session_id: uuid.UUID
    episode_id: uuid.UUID
    device_profile_id: uuid.UUID
    started_at: datetime
    received_at: datetime
    posture: Posture
    status: SessionStatus
    model_version: str
    n_beats_total: int
    n_beats_usable: int
    quality: dict
    #: Set when a stored estimate exists but is being withheld. Invariant 6: it names the
    #: limitation and refers on, and says nothing about what the estimate would have shown.
    trend_withheld: str | None = None
    trend: TrendEstimateOut | None = None
    rejection: RejectionOut | None = None
