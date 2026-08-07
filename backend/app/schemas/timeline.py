"""Patient timeline (GET /v1/episodes/{id}/timeline).

BUILD_SPEC 4.2: "Timeline responses must return estimates and cuff readings as **distinct types
with distinct field sets**, so a client cannot accidentally render one as the other."

That is invariant 1 expressed in the response shape. A client that reaches for
``item.systolic_mmhg`` on an estimate gets nothing, because the field does not exist on that
type — not a zero, not a null, nothing. The types below are pairwise disjoint apart from three
documented, deliberately-shared sets, and
``test_timeline_returns_estimates_and_readings_as_distinct_types`` enforces it.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import Field

from app.models.enums import (
    CuffSource,
    DeviationState,
    EventType,
    Posture,
    RejectionReason,
    TrendDirection,
)
from app.schemas.common import SyntheticFlag, TeraModel
from app.services import language

#: Fields every timeline item has because it is a timeline item, not because of what it holds.
STRUCTURAL_FIELDS = frozenset(
    {"record_type", "id", "occurred_at", "synthetic", "synthetic_notice"}
)

#: Both session-derived types name the session they came from. This is a link, not content —
#: it carries nothing that could be rendered as a measurement.
SESSION_LINK_FIELDS = frozenset({"session_id"})

#: The three event types are deliberately the same shape, discriminated by ``record_type``.
#: They record what the patient reported, and that is the same kind of thing in each case.
EVENT_FIELDS = frozenset({"payload", "event_type"})


class TimelineCuffReading(SyntheticFlag, TeraModel):
    """A cuff-confirmed reading. Real numerals, stated unit, confirmed by a person."""

    record_type: Literal["cuff_reading"] = "cuff_reading"
    id: uuid.UUID
    occurred_at: datetime
    systolic_mmhg: int
    diastolic_mmhg: int
    pulse_bpm: int | None
    unit: Literal["mmHg"] = "mmHg"
    source: CuffSource
    user_confirmed_at: datetime
    corrects_id: uuid.UUID | None = None
    cuff_badge: Literal[language.CUFF_BADGE] = language.CUFF_BADGE


class TimelineTrendEstimate(SyntheticFlag, TeraModel):
    """A trend estimate. No numerals that could read as a measurement.

    Note what is absent: no ``systolic_mmhg``, no ``diastolic_mmhg``, no ``unit``. ``direction``
    and ``interpretation`` are the intended rendering; ``magnitude_sd`` is present because the
    clinician view needs it, and BUILD_SPEC 5.4 forbids rendering it as though it were mmHg.
    """

    record_type: Literal["trend_estimate"] = "trend_estimate"
    id: uuid.UUID
    occurred_at: datetime
    session_id: uuid.UUID
    calibration_id: uuid.UUID
    direction: TrendDirection
    magnitude_sd: float
    confidence: float
    deviation_state: DeviationState
    interpretation: str
    estimate_badge: Literal[language.ESTIMATE_BADGE] = language.ESTIMATE_BADGE
    magnitude_notice: str = language.MAGNITUDE_NOTICE
    confidence_notice: str = language.CONFIDENCE_NOTICE


class TimelineRejectedSession(SyntheticFlag, TeraModel):
    """Invariant 3 — present, visible, never hidden."""

    record_type: Literal["rejected_session"] = "rejected_session"
    id: uuid.UUID
    occurred_at: datetime
    session_id: uuid.UUID
    rejection_reason: RejectionReason
    reason_text: str
    posture: Posture
    n_beats_total: int
    n_beats_usable: int
    retry_available: bool = True
    rejection_badge: Literal[language.REJECTED_BADGE] = language.REJECTED_BADGE


class TimelineEvent(SyntheticFlag, TeraModel):
    """A medication, symptom or red-flag report."""

    record_type: Literal["medication_event", "symptom_event", "red_flag_event"]
    id: uuid.UUID
    occurred_at: datetime
    event_type: EventType
    payload: dict[str, Any]


TimelineItem = Annotated[
    TimelineCuffReading | TimelineTrendEstimate | TimelineRejectedSession | TimelineEvent,
    Field(discriminator="record_type"),
]

#: The record types whose field sets must stay disjoint. Registered here so the test does not
#: have to be updated by hand when a type is added.
DISJOINT_RECORD_MODELS: tuple[type[TeraModel], ...] = (
    TimelineCuffReading,
    TimelineTrendEstimate,
    TimelineRejectedSession,
    TimelineEvent,
)


class TimelineOut(TeraModel):
    """GET /v1/episodes/{id}/timeline."""

    episode_id: uuid.UUID
    patient_pseudonym: str
    started_at: datetime
    ended_at: datetime | None
    #: Invariant 9 — true when any item in this timeline is synthetic, so a client can surface
    #: the notice at the page level as well as per record.
    contains_synthetic_data: bool
    synthetic_notice: str | None = None
    items: list[TimelineItem] = Field(
        default_factory=list, description="Newest first."
    )
