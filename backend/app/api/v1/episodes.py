"""Episode timeline and clinician summary."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.api.deps import (
    DbDep,
    PrincipalDep,
    load_episode,
    summary_rate_limit,
)
from app.logging_config import get_logger
from app.models import AuditAction, MonitoringEpisode
from app.schemas.common import SyntheticFlag
from app.schemas.summary import ClinicianSummaryOut, EpisodeListOut
from app.schemas.timeline import TimelineOut
from app.services import audit
from app.services import summary as summary_service
from app.services import timeline as timeline_service

router = APIRouter(prefix="/episodes", tags=["episodes"])
log = get_logger(__name__)


@router.get(
    "",
    response_model=EpisodeListOut,
    summary="Episodes visible to the caller, with at-a-glance system indicators",
)
def list_episodes(db: DbDep, principal: PrincipalDep) -> EpisodeListOut:
    """Clinician episode list (BUILD_SPEC 5.3 screen 2).

    The indicators are system states — session yield, cuff staleness, calibration age. Never
    physiological values, because BUILD_SPEC 5.1 reserves warning treatment for system states.
    """
    stmt = select(MonitoringEpisode)
    if principal.is_patient:
        stmt = stmt.where(MonitoringEpisode.patient_id == principal.patient_id)
    elif principal.is_clinician:
        stmt = stmt.where(MonitoringEpisode.reviewing_clinician_id == principal.user_id)
    # An admin sees everything; no additional filter.

    episodes = db.execute(stmt.order_by(MonitoringEpisode.started_at.desc())).scalars().all()
    items = [summary_service.build_list_item(db, episode=episode) for episode in episodes]
    contains_synthetic = any(item.synthetic for item in items)

    return EpisodeListOut(
        episodes=items,
        contains_synthetic_data=contains_synthetic,
        synthetic_notice=SyntheticFlag.notice_for(contains_synthetic),
    )


@router.get(
    "/{episode_id}/timeline",
    response_model=TimelineOut,
    summary="Patient timeline: readings, estimates, rejected sessions and events",
)
def get_timeline(
    episode_id: uuid.UUID, db: DbDep, principal: PrincipalDep
) -> TimelineOut:
    """Chronological record.

    Estimates and cuff readings come back as distinct types with distinct field sets
    (BUILD_SPEC 4.2), so a client cannot render one as the other.
    """
    episode = load_episode(episode_id, principal, db)
    result = timeline_service.build(db, episode=episode)

    audit.record(db, principal=principal, action=AuditAction.TIMELINE_VIEWED, target=episode.id)
    db.commit()

    log.info(
        "timeline_viewed",
        extra={"episode_id": str(episode.id), "item_count": len(result.items)},
    )
    return result


@router.get(
    "/{episode_id}/summary",
    response_model=ClinicianSummaryOut,
    summary="Clinician exception summary",
    status_code=status.HTTP_200_OK,
)
def get_summary(
    episode_id: uuid.UUID,
    db: DbDep,
    principal: Annotated[object, Depends(summary_rate_limit)],
) -> ClinicianSummaryOut:
    """Generate the exception summary and record that it was generated.

    Each generation appends a ``clinician_summary`` row rather than updating the previous one
    (invariant 5), so the record shows what was on screen at a given moment. ``viewed_at`` is
    set only when a clinician is the caller — a patient fetching it is not a clinical review.
    """
    episode = load_episode(episode_id, principal, db)

    # Proposal, page 4: the exception summary is a "role-protected clinician web view". A
    # patient owns the underlying records and can read every one of them on their own
    # timeline, so this is not about hiding data from them — the summary is written for a
    # clinician, in clinical shorthand, and is not the interface a patient should read their
    # own care through.
    #
    # 403 rather than 404: the patient already knows their episode exists, so refusing
    # discloses nothing, and 404 would be a lie that makes the client harder to debug.
    if principal.is_patient:
        audit.record(
            db,
            principal=principal,
            action=AuditAction.CLINICIAN_ACCESS_DENIED,
            target=episode.id,
        )
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the episode summary is a clinician view; your records are on your timeline",
        )

    document = summary_service.build(db, episode=episode)

    summary_service.persist(
        db, episode=episode, summary=document, viewed=principal.is_clinician
    )
    audit.record(
        db, principal=principal, action=AuditAction.SUMMARY_GENERATED, target=episode.id
    )
    db.commit()

    log.info(
        "summary_generated",
        extra={
            "episode_id": str(episode.id),
            "notable_change_count": len(document.notable_changes),
            "rejected_session_count": len(document.rejected_sessions),
        },
    )
    return document
