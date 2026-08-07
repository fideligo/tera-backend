"""POST /v1/events — medication, symptom or red-flag reports.

Invariant 8 governs the red-flag path: chest pain, severe breathlessness, severe headache,
visual disturbance, or new weakness or speech difficulty produce an immediate instruction to
seek emergency care, with no measurement offered and no estimate displayed.

**This endpoint is not what makes that happen.** The handset shows the instruction locally the
moment the symptom is selected, without waiting for a network round trip, because the invariant
says the path must not depend on network availability. What this endpoint does is record that
it happened. The ``emergency_instruction`` field in the response is a copy of what was shown,
so the record and the screen agree — not the source of it.
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.api.deps import DbDep, PrincipalDep, load_episode
from app.logging_config import get_logger
from app.models import AuditAction, EventType, MedicationEvent, RedFlagEvent, SymptomEvent
from app.schemas.clinical import EventCreate, EventOut
from app.schemas.common import SyntheticFlag
from app.services import audit, language

router = APIRouter(prefix="/events", tags=["events"])
log = get_logger(__name__)

_MODEL_FOR_TYPE = {
    EventType.MEDICATION: MedicationEvent,
    EventType.SYMPTOM: SymptomEvent,
    EventType.RED_FLAG: RedFlagEvent,
}


@router.post(
    "",
    response_model=EventOut,
    status_code=status.HTTP_201_CREATED,
    summary="Record a medication, symptom or red-flag event",
)
def create_event(body: EventCreate, db: DbDep, principal: PrincipalDep) -> EventOut:
    """Record one patient-reported event.

    Invariant 6: the response never advises. For a red flag it repeats the emergency
    instruction; for anything else it acknowledges the record and stops. No endpoint here
    interprets a symptom, and none of them says anything about medication.
    """
    episode = load_episode(body.episode_id, principal, db)

    model = _MODEL_FOR_TYPE[body.event_type]
    row = model(
        episode_id=episode.id,
        occurred_at=body.occurred_at,
        payload=body.payload,
        synthetic=body.synthetic,
    )
    db.add(row)
    db.flush()

    audit.record(db, principal=principal, action=AuditAction.EVENT_RECORDED, target=row.id)
    db.commit()

    # Symptom text and medication detail never reach the log (BUILD_SPEC 4.5) — the event type
    # and the row id are what an operator needs, and the redacting formatter drops the payload
    # even if someone adds it here later.
    log.info(
        "event_recorded",
        extra={
            "event_id": str(row.id),
            "episode_id": str(row.episode_id),
            "event_type": body.event_type.value,
        },
    )

    return EventOut(
        id=row.id,
        episode_id=row.episode_id,
        event_type=body.event_type,
        occurred_at=row.occurred_at,
        recorded_at=row.recorded_at,
        synthetic=row.synthetic,
        synthetic_notice=SyntheticFlag.notice_for(row.synthetic),
        emergency_instruction=(
            language.ACTION_SEEK_EMERGENCY_CARE
            if body.event_type is EventType.RED_FLAG
            else None
        ),
    )
